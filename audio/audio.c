#define _POSIX_C_SOURCE 200809L
#include <alsa/asoundlib.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define RATE 48000
#define PERIOD 128
static volatile sig_atomic_t running = 1;
static void stop(int signum) { (void)signum; running = 0; }

static int setup(snd_pcm_t **pcm, const char *name, snd_pcm_stream_t direction,
                 unsigned *channels) {
    int e = snd_pcm_open(pcm, name, direction, 0);
    if (e < 0) return e;
    snd_pcm_hw_params_t *p;
    snd_pcm_hw_params_alloca(&p);
    if ((e = snd_pcm_hw_params_any(*pcm, p)) < 0 ||
        (e = snd_pcm_hw_params_set_access(*pcm, p, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0 ||
        (e = snd_pcm_hw_params_set_format(*pcm, p, SND_PCM_FORMAT_S16_LE)) < 0 ||
        (e = snd_pcm_hw_params_set_rate(*pcm, p, RATE, 0)) < 0) return e;
    unsigned wanted[] = { direction == SND_PCM_STREAM_CAPTURE ? 1u : 2u,
                          direction == SND_PCM_STREAM_CAPTURE ? 2u : 1u };
    for (int i = 0; i < 2; i++) {
        if (snd_pcm_hw_params_set_channels(*pcm, p, wanted[i]) == 0) {
            *channels = wanted[i];
            break;
        }
    }
    if (!*channels) return -EINVAL;
    snd_pcm_uframes_t period = PERIOD;
    snd_pcm_uframes_t buffer = PERIOD * 4;
    if ((e = snd_pcm_hw_params_set_period_size_near(*pcm, p, &period, 0)) < 0 ||
        (e = snd_pcm_hw_params_set_buffer_size_near(*pcm, p, &buffer)) < 0 ||
        (e = snd_pcm_hw_params(*pcm, p)) < 0) return e;
    return snd_pcm_prepare(*pcm);
}

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: audio INPUT OUTPUT VOLUME_PERCENT\n");
        return 2;
    }
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    setvbuf(stdout, NULL, _IOLBF, 0);
    snd_pcm_t *in = NULL, *out = NULL;
    unsigned in_ch = 0, out_ch = 0;
    int e = setup(&in, argv[1], SND_PCM_STREAM_CAPTURE, &in_ch);
    if (e >= 0) e = setup(&out, argv[2], SND_PCM_STREAM_PLAYBACK, &out_ch);
    if (e < 0) {
        fprintf(stderr, "audio setup: %s\n", snd_strerror(e));
        if (in) snd_pcm_close(in);
        if (out) snd_pcm_close(out);
        return 1;
    }
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);
    int16_t input[PERIOD * 2], output[PERIOD * 2];
    double gain = atof(argv[3]) / 100.0;
    double sum = 0, peak = 0, next = now() + .1;
    unsigned count = 0;
    char command[32];
    size_t command_len = 0;
    puts("READY");
    while (running) {
        snd_pcm_sframes_t frames = snd_pcm_readi(in, input, PERIOD);
        if (frames < 0) {
            if (snd_pcm_recover(in, (int)frames, 1) < 0) break;
            continue;
        }
        for (snd_pcm_sframes_t i = 0; i < frames; i++) {
            int32_t mono = in_ch == 1 ? input[i] :
                ((int32_t)input[i * 2] + input[i * 2 + 1]) / 2;
            double normalized = mono / 32768.0;
            sum += normalized * normalized;
            if (fabs(normalized) > peak) peak = fabs(normalized);
            count++;
            for (unsigned c = 0; c < out_ch; c++) {
                int32_t sample = (int32_t)lrint((in_ch == 2 && out_ch == 2 ?
                    input[i * 2 + c] : mono) * gain);
                if (sample > 32767) sample = 32767;
                if (sample < -32768) sample = -32768;
                output[i * out_ch + c] = (int16_t)sample;
            }
        }
        snd_pcm_sframes_t written = 0;
        while (written < frames && running) {
            snd_pcm_sframes_t n = snd_pcm_writei(out, output + written * out_ch, frames - written);
            if (n < 0) {
                if (snd_pcm_recover(out, (int)n, 1) < 0) { running = 0; break; }
            } else written += n;
        }
        double t = now();
        if (t >= next && count) {
            printf("LEVEL %.4f %.4f\n", sqrt(sum / count), peak);
            sum = peak = 0;
            count = 0;
            next = t + .1;
        }
        char ch;
        while (read(STDIN_FILENO, &ch, 1) == 1) {
            if (ch == '\n') {
                command[command_len] = 0;
                if (strncmp(command, "VOLUME ", 7) == 0) {
                    char *end;
                    double v = strtod(command + 7, &end);
                    if (*end == 0 && v >= 0 && v <= 150) gain = v / 100.0;
                }
                command_len = 0;
            } else if (command_len < sizeof(command) - 1) command[command_len++] = ch;
            else command_len = 0;
        }
    }
    snd_pcm_close(in);
    snd_pcm_close(out);
    return 0;
}

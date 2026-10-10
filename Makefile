CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra
audio/audio: audio/audio.c
	$(CC) $(CFLAGS) -o $@ $< -lasound -lm

.PHONY: clean
clean:
	rm -f audio/audio

#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;

static void stop_handler(int signo)
{
    (void)signo;
    running = 0;
}

static double monotonic_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

int main(int argc, char **argv)
{
    const size_t chunk_mib = 64;
    const size_t chunk_size = chunk_mib * 1024 * 1024;
    long page_size = sysconf(_SC_PAGESIZE);
    unsigned long target_mib;
    unsigned long timeout_seconds;
    unsigned long retouch_seconds;
    size_t chunk_count;
    unsigned char **chunks;
    size_t allocated = 0;
    double started;

    if (argc != 4) {
        fprintf(stderr, "usage: %s TARGET_MIB TIMEOUT_SECONDS RETOUCH_SECONDS\n", argv[0]);
        return 2;
    }
    target_mib = strtoul(argv[1], NULL, 10);
    timeout_seconds = strtoul(argv[2], NULL, 10);
    retouch_seconds = strtoul(argv[3], NULL, 10);
    if (target_mib < chunk_mib || timeout_seconds == 0 || retouch_seconds == 0 || page_size <= 0) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    signal(SIGINT, stop_handler);
    signal(SIGTERM, stop_handler);
    FILE *oom = fopen("/proc/self/oom_score_adj", "w");
    if (oom != NULL) {
        fputs("1000\n", oom);
        fclose(oom);
    }

    chunk_count = (target_mib + chunk_mib - 1) / chunk_mib;
    chunks = calloc(chunk_count, sizeof(*chunks));
    if (chunks == NULL) {
        perror("calloc");
        return 1;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("READY pid=%ld target_mib=%lu timeout_seconds=%lu retouch_seconds=%lu\n",
           (long)getpid(), target_mib, timeout_seconds, retouch_seconds);

    for (size_t i = 0; i < chunk_count && running; ++i) {
        size_t wanted = chunk_size;
        size_t remaining_mib = target_mib - allocated * chunk_mib;
        if (remaining_mib < chunk_mib)
            wanted = remaining_mib * 1024 * 1024;
        chunks[i] = mmap(NULL, wanted, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (chunks[i] == MAP_FAILED) {
            chunks[i] = NULL;
            fprintf(stderr, "MMAP_FAILED allocated_mib=%zu errno=%d %s\n",
                    allocated * chunk_mib, errno, strerror(errno));
            break;
        }
        for (size_t offset = 0; offset < wanted && running; offset += (size_t)page_size)
            chunks[i][offset] = (unsigned char)(i + offset);
        ++allocated;
        printf("ALLOCATED_MIB %zu\n",
               allocated == chunk_count ? target_mib : allocated * chunk_mib);
    }

    started = monotonic_seconds();
    while (running && monotonic_seconds() - started < (double)timeout_seconds) {
        for (size_t i = 0; i < allocated && running; ++i) {
            size_t wanted = chunk_size;
            if (i + 1 == chunk_count && target_mib % chunk_mib)
                wanted = (target_mib % chunk_mib) * 1024 * 1024;
            for (size_t offset = 0; offset < wanted; offset += (size_t)page_size)
                chunks[i][offset] ^= 1;
        }
        for (unsigned long second = 0; second < retouch_seconds && running; ++second)
            sleep(1);
    }

    printf("STOP allocated_mib=%zu reason=%s\n",
           allocated == chunk_count ? (size_t)target_mib : allocated * chunk_mib,
           running ? "timeout" : "signal");
    for (size_t i = 0; i < allocated; ++i) {
        size_t wanted = chunk_size;
        if (i + 1 == chunk_count && target_mib % chunk_mib)
            wanted = (target_mib % chunk_mib) * 1024 * 1024;
        munmap(chunks[i], wanted);
    }
    free(chunks);
    return 0;
}

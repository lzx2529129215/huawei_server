#define _GNU_SOURCE
#include <sys/mman.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

static volatile sig_atomic_t running = 1;
static void stop(int signal) { (void)signal; running = 0; }
static double seconds(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}
int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: pressure MIB LIFETIME_SECONDS\n"); return 2; }
    unsigned long mib = strtoul(argv[1], NULL, 10), ttl = strtoul(argv[2], NULL, 10);
    if (!mib || mib > 131072 || !ttl || ttl > 3600) return 2;
    const size_t chunk = 64 * 1024 * 1024;
    size_t count = (mib + 63) / 64;
    unsigned char **chunks = calloc(count, sizeof(*chunks));
    if (!chunks) return 1;
    signal(SIGINT, stop); signal(SIGTERM, stop);
    setvbuf(stdout, NULL, _IOLBF, 0);
    const double deadline = seconds() + ttl;
    uint64_t state = 0x9e3779b97f4a7c15ULL;
    size_t allocated = 0;
    printf("READY pid=%ld target_mib=%lu\n", (long)getpid(), mib);
    for (size_t i = 0; i < count && running && seconds() < deadline; ++i) {
        size_t bytes = i + 1 == count ? (mib - i * 64) * 1024 * 1024 : chunk;
        chunks[i] = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (chunks[i] == MAP_FAILED) { chunks[i] = NULL; printf("FAILED errno=%d\n", errno); break; }
        uint64_t *words = (uint64_t *)chunks[i];
        for (size_t j = 0; j < bytes / 8 && running; ++j) {
            state ^= state >> 12; state ^= state << 25; state ^= state >> 27;
            words[j] = state * 2685821657736338717ULL;
            if (!(j & 8191) && seconds() >= deadline) running = 0;
        }
        ++allocated;
        if (running) printf("ALLOCATED_MIB %zu\n", i + 1 == count ? (size_t)mib : allocated * 64);
    }
    while (running && seconds() < deadline) {
        for (size_t i = 0; i < allocated && running; ++i) {
            size_t bytes = i + 1 == count ? (mib - i * 64) * 1024 * 1024 : chunk;
            for (size_t j = 0; j < bytes; j += 4096) chunks[i][j] ^= 1;
        }
        sleep(2);
    }
    for (size_t i = 0; i < allocated; ++i) {
        size_t bytes = i + 1 == count ? (mib - i * 64) * 1024 * 1024 : chunk;
        munmap(chunks[i], bytes);
    }
    free(chunks); puts("STOP clean=true"); return 0;
}

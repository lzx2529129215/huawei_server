#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t started = 0;
static volatile sig_atomic_t retouch_requests = 0;
static volatile sig_atomic_t running = 1;

static void handle_signal(int sig)
{
    if (sig == SIGUSR1) started = 1;
    else if (sig == SIGUSR2) ++retouch_requests;
    else running = 0;
}

static size_t bytes_from_mib(const char *arg)
{
    char *end = NULL;
    unsigned long value = strtoul(arg, &end, 10);
    if (!arg[0] || !end || *end || value == 0 || value > 1024) return 0;
    return (size_t)value * 1024 * 1024;
}

static void fault_counts(long *minor, long *major)
{
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) != 0) {
        perror("getrusage");
        exit(1);
    }
    *minor = usage.ru_minflt;
    *major = usage.ru_majflt;
}

int main(int argc, char **argv)
{
    if (argc != 5) {
        fprintf(stderr, "usage: %s FILE_PATH FILE_MIB ANON_MIB TIMEOUT_SECONDS\n", argv[0]);
        return 2;
    }
    const size_t file_bytes = bytes_from_mib(argv[2]);
    const size_t anon_bytes = bytes_from_mib(argv[3]);
    const unsigned long timeout = strtoul(argv[4], NULL, 10);
    const long page_size = sysconf(_SC_PAGESIZE);
    if (!file_bytes || !anon_bytes || timeout < 30 || page_size <= 0) return 2;
    struct sigaction action = {.sa_handler = handle_signal};
    sigemptyset(&action.sa_mask);
    sigaction(SIGUSR1, &action, NULL);
    sigaction(SIGUSR2, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("READY pid=%ld file_mib=%s anon_mib=%s\n", (long)getpid(), argv[2], argv[3]);
    for (int i = 0; i < 300 && running && !started; ++i) usleep(100000);
    if (!running || !started) return 3;

    int fd = open(argv[1], O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0 || ftruncate(fd, (off_t)file_bytes) != 0) {
        perror("file_setup");
        if (fd >= 0) close(fd);
        return 1;
    }
    volatile uint8_t *file_map = mmap(NULL, file_bytes, PROT_READ | PROT_WRITE,
                                      MAP_SHARED, fd, 0);
    volatile uint8_t *anon_map = mmap(NULL, anon_bytes, PROT_READ | PROT_WRITE,
                                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (file_map == MAP_FAILED || anon_map == MAP_FAILED) {
        perror("mmap");
        if (file_map != MAP_FAILED) munmap((void *)file_map, file_bytes);
        if (anon_map != MAP_FAILED) munmap((void *)anon_map, anon_bytes);
        close(fd);
        return 1;
    }
    for (size_t offset = 0; offset < file_bytes && running; offset += (size_t)page_size)
        file_map[offset] = (uint8_t)(offset / (size_t)page_size);
    if (msync((void *)file_map, file_bytes, MS_SYNC) != 0) {
        perror("msync");
        return 1;
    }
    for (size_t offset = 0; offset < anon_bytes && running; offset += (size_t)page_size)
        anon_map[offset] = (uint8_t)(offset / (size_t)page_size);
    if (!running) return 1;
    printf("ALLOCATED file_bytes=%zu anon_bytes=%zu\n", file_bytes, anon_bytes);

    time_t deadline = time(NULL) + (time_t)timeout;
    unsigned long checksum = 0;
    while (running && time(NULL) < deadline) {
        if (retouch_requests > 0) {
            --retouch_requests;
            long min0, maj0, min1, maj1, min2, maj2;
            fault_counts(&min0, &maj0);
            for (size_t offset = 0; offset < file_bytes; offset += (size_t)page_size)
                checksum += file_map[offset];
            fault_counts(&min1, &maj1);
            for (size_t offset = 0; offset < anon_bytes; offset += (size_t)page_size)
                anon_map[offset] ^= 1;
            fault_counts(&min2, &maj2);
            printf("RETOUCH file_minor=%ld file_major=%ld anon_minor=%ld anon_major=%ld checksum=%lu\n",
                   min1 - min0, maj1 - maj0, min2 - min1, maj2 - maj1, checksum);
        }
        usleep(100000);
    }
    printf("STOP reason=%s\n", running ? "timeout" : "signal");
    munmap((void *)file_map, file_bytes);
    munmap((void *)anon_map, anon_bytes);
    close(fd);
    return 0;
}

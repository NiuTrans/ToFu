// LD_PRELOAD shim for tests/tofudb/test_wal_stall_liveness.py. While the file
// named by WAL_STALL_TRIGGER exists, every write to a descriptor whose path
// ends in ".wal" blocks, modelling a stalled network-volume write; the first
// blocked write creates "<trigger>.entered". Reads, syncs and other files
// pass through unchanged.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

static int stall_armed(void) {
    const char *trigger = getenv("WAL_STALL_TRIGGER");
    struct stat st;
    return trigger != NULL && stat(trigger, &st) == 0;
}

static int is_wal(int fd) {
    char link[64], path[4096];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof path - 1);
    if (n < 4) return 0;
    path[n] = 0;
    return strcmp(path + n - 4, ".wal") == 0;
}

static void maybe_stall(int fd) {
    if (!stall_armed() || !is_wal(fd)) return;
    char entered[4096];
    snprintf(entered, sizeof entered, "%s.entered", getenv("WAL_STALL_TRIGGER"));
    int marker = open(entered, O_CREAT | O_WRONLY | O_CLOEXEC, 0600);
    if (marker >= 0) close(marker);
    struct timespec pause = {0, 5 * 1000 * 1000};
    while (stall_armed()) nanosleep(&pause, NULL);
}

ssize_t pwrite64(int fd, const void *buf, size_t count, off_t offset) {
    static ssize_t (*real)(int, const void *, size_t, off_t);
    if (!real) real = dlsym(RTLD_NEXT, "pwrite64");
    maybe_stall(fd);
    return real(fd, buf, count, offset);
}

ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset) {
    return pwrite64(fd, buf, count, offset);
}

ssize_t write(int fd, const void *buf, size_t count) {
    static ssize_t (*real)(int, const void *, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "write");
    if (fd > 2) maybe_stall(fd);
    return real(fd, buf, count);
}

ssize_t writev(int fd, const struct iovec *iov, int iovcnt) {
    static ssize_t (*real)(int, const struct iovec *, int);
    if (!real) real = dlsym(RTLD_NEXT, "writev");
    if (fd > 2) maybe_stall(fd);
    return real(fd, iov, iovcnt);
}

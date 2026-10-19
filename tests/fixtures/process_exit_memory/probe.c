/* Owned process/exit-memory fixtures; no network, files or product state. */
#include <assert.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t signaled;
static void received(int value) { signaled = value; }
static void *sleeper(void *unused) { (void)unused; sleep(30); return NULL; }
static void allocate(size_t bytes) {
    volatile char *memory = malloc(bytes);
    assert(memory);
    for (size_t offset = 0; offset < bytes; offset += 4096) memory[offset] = 1;
    free((void *)memory);
}
static pid_t spawn(const char *self, const char *mode) {
    pid_t child = fork(); assert(child >= 0);
    if (!child) { execl(self, self, mode, NULL); _exit(111); }
    return child;
}
static void *measured_thread(void *bytes) {
    allocate((size_t)bytes); return NULL;
}
static void *measured_waiter(void *ready_fd) {
    allocate(32u * 1024 * 1024);
    assert(write(*(int *)ready_fd, "r", 1) == 1);
    for (;;) pause();
    return NULL;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    const char *mode = argv[1];
    if (!strcmp(mode, "child-short")) { allocate(32u * 1024 * 1024); return 7; }
    if (!strcmp(mode, "child-wait")) { sleep(30); return 0; }
    if (!strcmp(mode, "child-reexec")) { execl(argv[0], argv[0], "child-short", NULL); return 111; }
    if (!strcmp(mode, "child-threaded-memory") || !strcmp(mode, "child-nested-threaded")) {
        pthread_t first, second;
        assert(!pthread_create(&first, NULL, measured_thread, (void *)(32u * 1024 * 1024)));
        assert(!pthread_create(&second, NULL, measured_thread, (void *)(8u * 1024 * 1024)));
        assert(!pthread_join(first, NULL)); assert(!pthread_join(second, NULL));
        if (!strcmp(mode, "child-nested-threaded")) {
            pid_t leaf = spawn(argv[0], "child-short"); int status;
            assert(waitpid(leaf, &status, 0) == leaf);
            assert(WIFEXITED(status) && WEXITSTATUS(status) == 7);
        }
        return 7;
    }
    if (!strcmp(mode, "child-killed-threads")) {
        int fd = atoi(getenv("TOFU_EXIT_PROBE_READY_FD"));
        pthread_t thread; assert(!pthread_create(&thread, NULL, measured_waiter, &fd));
        for (;;) pause();
    }
    if (!strcmp(mode, "child-threads")) {
        pthread_t thread; assert(!pthread_create(&thread, NULL, sleeper, NULL));
        pthread_join(thread, NULL); return 0;
    }
    if (!strcmp(mode, "signal")) {
        signal(SIGUSR1, received); raise(SIGUSR1);
        assert(signaled == SIGUSR1); puts("signal delivered"); return 0;
    }
    if (!strcmp(mode, "threads")) {
        pthread_t thread; assert(!pthread_create(&thread, NULL, sleeper, NULL));
        pthread_cancel(thread); pthread_join(thread, NULL); return 0;
    }
    if (!strcmp(mode, "short") || !strcmp(mode, "parallel") || !strcmp(mode, "threaded")
        || !strcmp(mode, "nested-threaded") || !strcmp(mode, "kill-threaded")) {
        volatile char *parent = malloc(128u * 1024 * 1024); assert(parent);
        for (size_t offset = 0; offset < 128u * 1024 * 1024; offset += 4096) parent[offset] = 1;
        int count = !strcmp(mode, "parallel") ? 4 : 1;
        int ready[2]; int killed = !strcmp(mode, "kill-threaded");
        if (killed) {
            assert(!pipe(ready)); char fd[32]; snprintf(fd, sizeof(fd), "%d", ready[1]);
            assert(!setenv("TOFU_EXIT_PROBE_READY_FD", fd, 1));
        }
        const char *child_mode = !strcmp(mode, "threaded") ? "child-threaded-memory" :
            !strcmp(mode, "nested-threaded") ? "child-nested-threaded" :
            killed ? "child-killed-threads" : "child-short";
        pid_t children[4]; for (int i = 0; i < count; ++i) children[i] = spawn(argv[0], child_mode);
        if (killed) {
            close(ready[1]); char byte; assert(read(ready[0], &byte, 1) == 1); close(ready[0]);
            assert(!kill(children[0], SIGKILL));
        }
        for (int i = 0; i < count; ++i) {
            int status; assert(waitpid(children[i], &status, 0) == children[i]);
            if (killed) assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
            else assert(WIFEXITED(status) && WEXITSTATUS(status) == 7);
        }
        printf("parent %ld joined %d children\n", (long)getpid(), count);
        free((void *)parent); return 0;
    }
    const char *child_mode = !strcmp(mode, "timeout") ? "child-wait" :
        !strcmp(mode, "reexec") ? "child-reexec" : "child-threads";
    pid_t child = spawn(argv[0], child_mode);
    printf("owned %ld %ld\n", (long)getpid(), (long)child); fflush(stdout);
    int status; assert(waitpid(child, &status, 0) == child);
    return 0;
}

/* Fixed stdin/stdout to Unix-socket relay for QEMU restricted guestfwd. */
#include <errno.h>
#include <poll.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int write_all(int fd, const char *buffer, size_t size) {
    while (size > 0) {
        ssize_t written = write(fd, buffer, size);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return -1;
        }
        buffer += written;
        size -= (size_t)written;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "--socket") != 0) {
        return 2;
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    size_t path_size = strlen(argv[2]);
    if (path_size == 0 || path_size >= sizeof(address.sun_path)) {
        return 2;
    }
    memcpy(address.sun_path, argv[2], path_size + 1);
    int upstream = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (upstream < 0 || connect(upstream, (struct sockaddr *)&address,
                                offsetof(struct sockaddr_un, sun_path) + path_size + 1) != 0) {
        if (upstream >= 0) {
            close(upstream);
        }
        return 1;
    }

    struct pollfd descriptors[2] = {
        {.fd = STDIN_FILENO, .events = POLLIN},
        {.fd = upstream, .events = POLLIN},
    };
    char buffer[65536];
    while (descriptors[1].fd >= 0) {
        int ready = poll(descriptors, 2, -1);
        if (ready < 0 && errno == EINTR) {
            continue;
        }
        if (ready < 0) {
            close(upstream);
            return 1;
        }
        if (descriptors[0].fd >= 0 &&
            (descriptors[0].revents & (POLLIN | POLLHUP | POLLERR))) {
            ssize_t size = read(STDIN_FILENO, buffer, sizeof(buffer));
            if (size > 0) {
                if (write_all(upstream, buffer, (size_t)size) != 0) {
                    close(upstream);
                    return 1;
                }
            } else {
                shutdown(upstream, SHUT_WR);
                descriptors[0].fd = -1;
            }
        }
        if (descriptors[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            ssize_t size = read(upstream, buffer, sizeof(buffer));
            if (size > 0) {
                if (write_all(STDOUT_FILENO, buffer, (size_t)size) != 0) {
                    close(upstream);
                    return 1;
                }
            } else {
                close(upstream);
                descriptors[1].fd = -1;
            }
        }
    }
    return 0;
}

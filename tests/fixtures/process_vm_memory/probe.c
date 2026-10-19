#define _GNU_SOURCE
#include <sched.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

static int child(void *opaque) {
    int *fds=opaque;char byte;close(fds[1]);
    if(read(fds[0],&byte,1)!=1)_exit(5);
    _exit(0);
}
int main(int argc,char **argv) {
    int fds[2];if(pipe(fds))return 2;
    char *memory=malloc(8<<20),*stack=malloc(1<<20);
    if(!memory||!stack)return 3;
    memset(memory,42,8<<20);pid_t pid;
    if(argc>1&&!strcmp(argv[1],"shared"))pid=clone(child,stack+(1<<20),CLONE_VM|SIGCHLD,fds);
    else {pid=fork();if(pid==0)child(fds);}
    if(pid<0)return 4;
    printf("{\"parent\":%d,\"child\":%d}\n",getpid(),pid);fflush(stdout);
    char byte;if(read(0,&byte,1)!=1) {
        if(argc>1&&!strcmp(argv[1],"hold"))for(;;)pause();
        byte='x';
    }
    if(write(fds[1],&byte,1)!=1)return 6;
    int status;waitpid(pid,&status,0);free(memory);free(stack);
    return !WIFEXITED(status)||WEXITSTATUS(status)!=0;
}

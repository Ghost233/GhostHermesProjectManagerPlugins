/* Kernel identities for the private Mac controller socket. */
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/proc_info.h>
#include <libproc.h>
#include <unistd.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void json_string(const char *value) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p < 32) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

int main(int argc, char **argv) {
    pid_t pid;
    if (argc == 2 && strcmp(argv[1], "peer") == 0) {
        socklen_t size = sizeof(pid);
        if (getsockopt(3, SOL_LOCAL, LOCAL_PEERPID, &pid, &size) != 0 || size != sizeof(pid)) return 1;
    } else if (argc == 3 && strcmp(argv[1], "process") == 0) {
        char *end;
        errno = 0;
        long parsed = strtol(argv[2], &end, 10);
        if (errno || *end || parsed <= 0 || parsed > INT_MAX) return 1;
        pid = (pid_t)parsed;
    } else return 1;
    struct proc_bsdinfo info;
    char executable[PROC_PIDPATHINFO_MAXSIZE];
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) != sizeof(info)
        || info.pbi_pid != (uint32_t)pid || info.pbi_uid != getuid()
        || proc_pidpath(pid, executable, sizeof(executable)) <= 0) return 1;
    printf("{\"pid\":%d,\"start_sec\":\"%llu\",\"start_usec\":\"%llu\",\"executable\":", pid,
        (unsigned long long)info.pbi_start_tvsec, (unsigned long long)info.pbi_start_tvusec);
    json_string(executable);
    puts("}");
    return 0;
}

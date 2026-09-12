// Generate a minor-page-fault storm of a controlled size, the way a Wine host
// startup does: mmap anonymous memory, touch every page once (forcing the kernel
// to allocate and clear_page it), munmap, repeat. No file I/O, no major faults.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>

static double secs = 8.0;
static size_t chunk = 64u << 20;   // 64 MB per cycle, like the 65404 KB regions
                                   // seen in the Wine host's maps
static volatile int stop = 0;
static long total_pages = 0;
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;

static void *worker(void *arg) {
    (void)arg;
    long pages = 0, ps = sysconf(_SC_PAGESIZE);
    while (!stop) {
        char *p = mmap(NULL, chunk, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) { usleep(1000); continue; }
        for (size_t off = 0; off < chunk; off += ps) { p[off] = 1; pages++; }
        munmap(p, chunk);
    }
    pthread_mutex_lock(&lk); total_pages += pages; pthread_mutex_unlock(&lk);
    return NULL;
}

#define MAX_THREADS 64

int main(int argc, char **argv) {
    int nthreads = 4;
    if (argc > 1) secs = atof(argv[1]);
    if (argc > 2) nthreads = atoi(argv[2]);
    // Unclamped, `faultgen 1 100` wrote 36 pthread_t past the end of t[] and died in
    // the stack protector -- after a full run, so the fault numbers it had just printed
    // looked perfectly usable.
    if (nthreads < 1) nthreads = 1;
    if (nthreads > MAX_THREADS) {
        fprintf(stderr, "faultgen: capping %d threads at %d\n", nthreads, MAX_THREADS);
        nthreads = MAX_THREADS;
    }
    pthread_t t[MAX_THREADS];
    struct timespec a, b;
    clock_gettime(CLOCK_MONOTONIC, &a);
    for (int i = 0; i < nthreads; i++) pthread_create(&t[i], NULL, worker, NULL);
    struct timespec ts = { (time_t)secs, (long)((secs - (long)secs) * 1e9) };
    nanosleep(&ts, NULL);
    stop = 1;
    for (int i = 0; i < nthreads; i++) pthread_join(t[i], NULL);
    clock_gettime(CLOCK_MONOTONIC, &b);
    double el = (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) / 1e9;
    fprintf(stderr, "faultgen: %ld pages in %.2fs = %.0f faults/s (%.2f GB/s zeroed)\n",
            total_pages, el, total_pages / el, total_pages * 4096.0 / el / 1e9);
    return 0;
}

/*
Trying to test intel math function speed.
*/

#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include "mkl.h"
// #include "amdlibm.h"
#include <time.h>
#define VEC 100000
#define LOOPS 1000
#define SCALE 0.1
#define PREC 10000000

float min = 0;  // use uniform dist between (min,max), affects math funcs
float max = 1;

int main(int argc, char** argv)
{
    double x[VEC], a[VEC], b[VEC], c[VEC];
    float xs[VEC], as[VEC], bs[VEC], cs[VEC];
    srand(time(NULL));
    int i, j;
    clock_t begin, end;

    printf("Performing %d loops on vectors of length %d\n", LOOPS, VEC);

    // get numeric command-line arguments
    if( argc>1 )
        sscanf(argv[1], "%f", &min);
    if( argc>2 )
        sscanf(argv[2], "%f", &max);
    printf("Using random numbers between (%f, %f)\n\n", min, max);

    begin = clock();
    for(i = 0; i < VEC; i++){
        x[i] = (double) (max - min) * (rand() % PREC) / PREC + min;
        xs[i] = (float) x[i];
    }
    end = clock();

    printf("random generation: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);
    // /opt/intel/compilers_and_libraries_2017.0.098/linux/mkl/include/

    // tanh (double)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vdTanh(VEC, x, a);
    }
    end = clock();
    printf("vdTanh: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // tanh (single)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vsTanh(VEC, xs, as);
    }
    end = clock();
    printf("vsTanh: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // expm1 (double)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vdExpm1(VEC, x, b);
    }
    end = clock();
    printf("vdExpm1: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // expm1 (single)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vsExpm1(VEC, xs, bs);
    }
    end = clock();
    printf("vsExpm1: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // sin (double)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vdSin(VEC, x, c);
    }
    end = clock();
    printf("vdSin: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // sin (single)
    begin = clock();
    for(j = 0; j < LOOPS; j++){
        vsSin(VEC, xs, cs);
    }
    end = clock();
    printf("vsSin: %f s\n", (double) (end - begin) / CLOCKS_PER_SEC);

    // make sure it actually computes the values.
    printf("\nMake sure the values are actually computed:\n");
    printf("x[0]: %f, a[0]: %f, b[0]: %f, c[0]: %f\n", x[0], a[0], b[0], c[0]);
    printf("xs[0]: %f, as[0]: %f, bs[0]: %f, cs[0]: %f\n", xs[0], as[0], bs[0], cs[0]);

    return 0;
}

//! Throughput benchmark for the Snowflake-style ID generator.
//!
//! Run with:  `cargo bench -p url-shortener --bench id_gen`
//! HTML report: `target/criterion/report/index.html`
//!
//! KEY THING TO READ OFF THE NUMBERS
//! ---------------------------------
//! `next_id` is gated by the 12-bit sequence field: at most `MAX_SEQUENCE`
//! (4096) ids per node per millisecond. A tight loop blows past that budget and
//! the generator spin-waits for the next wall-clock ms. So the ceiling is:
//!
//!     4096 ids/ms * 1000 ms/s = 4_096_000 ids/sec  PER NODE
//!
//! Criterion reports `Throughput::Elements`, so watch the "elem/s" line — it
//! should plateau near ~4.1 Melem/s no matter how fast the CPU is, and adding
//! threads does NOT raise it (one node = one shared 4096/ms budget; extra
//! threads just add CAS contention). Run multiple nodes for more aggregate.

// Pull the module source in directly — the crate has no lib target, and id_gen
// only depends on std, so this compiles standalone. The `#[cfg(test)]` tests
// inside (which need proptest) are excluded under the bench profile.
//
// `allow(dead_code)`: this bench exercises only part of the API (next_id +
// assemble_id + base62_encode). Items the binary uses but the bench doesn't —
// `decode`/`IdParts` — would otherwise warn here. True dead code is still
// caught by the lib/bin build.
#[path = "../src/id_gen.rs"]
#[allow(dead_code)]
mod id_gen;

use std::hint::black_box;
use std::sync::Arc;
use std::thread;
use std::time::Instant;

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};

use id_gen::IdGenerator;

/// Single-threaded throughput of the two public hot paths.
fn bench_single_thread(c: &mut Criterion) {
    let mut group = c.benchmark_group("id_gen/single_thread");
    group.throughput(Throughput::Elements(1));

    let generator = IdGenerator::new(1);
    group.bench_function("next_id", |b| b.iter(|| black_box(generator.next_id())));

    // next_slug = next_id + base62 encode; the gap between the two lines is the
    // cost of the String allocation + base62 loop.
    group.bench_function("next_slug", |b| b.iter(|| black_box(generator.next_slug())));

    group.finish();
}

/// Aggregate throughput across N threads hammering ONE shared generator.
///
/// `iter_custom` lets us time the whole fan-out ourselves: each thread does
/// `iters / threads` calls, so total work ≈ `iters` and criterion's per-element
/// throughput reflects the *aggregate* rate across all threads.
fn bench_contended(c: &mut Criterion) {
    let mut group = c.benchmark_group("id_gen/contended");
    group.throughput(Throughput::Elements(1));

    for threads in [1usize, 2, 4, 8] {
        group.bench_with_input(BenchmarkId::from_parameter(threads), &threads, |b, &n| {
            let generator = Arc::new(IdGenerator::new(1));
            b.iter_custom(|iters| {
                let per_thread = iters / n as u64;
                let start = Instant::now();
                let handles: Vec<_> = (0..n)
                    .map(|_| {
                        let g = Arc::clone(&generator);
                        thread::spawn(move || {
                            for _ in 0..per_thread {
                                black_box(g.next_id());
                            }
                        })
                    })
                    .collect();
                for h in handles {
                    h.join().expect("worker thread panicked");
                }
                start.elapsed()
            });
        });
    }

    group.finish();
}

/// Raw compute ceiling of the bit-packing + base62 encode, with the wall clock
/// taken out of the loop. This is what `next_id` *could* do if it weren't gated
/// by the 12-bit/ms sequence — expect this to be an order of magnitude faster.
fn bench_raw_compute(c: &mut Criterion) {
    let mut group = c.benchmark_group("id_gen/raw_compute");
    group.throughput(Throughput::Elements(1));

    let sample = IdGenerator::assemble_id(black_box(1_700_000_000), black_box(2_345), black_box(7));

    group.bench_function("assemble_id", |b| {
        b.iter(|| {
            IdGenerator::assemble_id(black_box(1_700_000_000), black_box(2_345), black_box(7))
        })
    });

    group.bench_function("base62_encode", |b| {
        b.iter(|| IdGenerator::base62_encode(black_box(sample)))
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_single_thread,
    bench_contended,
    bench_raw_compute
);
criterion_main!(benches);

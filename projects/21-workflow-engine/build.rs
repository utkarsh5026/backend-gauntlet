//! Compile the gRPC `.proto` contract into Rust at build time.
//!
//! We point the protobuf compiler at the vendored `protoc` binary so the build works
//! on any machine without a system protobuf install. Both the server (this engine)
//! and the client (workers, tests, the boss-fight load generator) are generated from
//! the one contract — a worker is just a gRPC client that long-polls for tasks.

fn main() -> anyhow::Result<()> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    // SAFETY: single-threaded build script, set before any codegen runs.
    std::env::set_var("PROTOC", protoc);

    tonic_prost_build::configure()
        .build_client(true)
        .build_server(true)
        .compile_protos(&["proto/workflow.proto"], &["proto"])?;

    Ok(())
}

//! Compile the gRPC `.proto` contract into Rust at build time.
//!
//! We point the protobuf compiler at the vendored `protoc` binary so the build
//! works on any machine without a system protobuf install.

fn main() -> anyhow::Result<()> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    // SAFETY: single-threaded build script, set before any codegen runs.
    std::env::set_var("PROTOC", protoc);

    tonic_prost_build::configure()
        // We only generate the server side; callers bring their own client.
        .build_client(false)
        .build_server(true)
        .compile_protos(&["proto/ratelimit.proto"], &["proto"])?;

    Ok(())
}

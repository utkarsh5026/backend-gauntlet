//! `ffmpeg` / `ffprobe` plumbing — the one place we shell out to the codec toolbox.
//!
//! This module is **fully wired** on purpose: rebuilding an H.264 encoder is not
//! the exercise. What *is* the exercise is everything around the encoder — *where*
//! to cut (V1), *how* to schedule the cuts (V2), running them in parallel and
//! idempotently (V3), and *how* to glue the results back together seamlessly (V4).
//! So the actual `-c:v libx264 …` invocation is a subprocess call here; the
//! orchestration is yours.
//!
//! `run` executes a command and turns a non-zero exit into an `AppError::Ffmpeg`
//! carrying stderr. `probe_keyframes` / `probe_duration` are the inputs V1 plans
//! against.

use tokio::process::Command;

use crate::error::AppError;

/// Run an external command to completion, capturing stderr on failure.
///
/// `args` is passed as-is; the caller builds the full argument vector (this is
/// where V3's *deterministic* encode flags will be assembled).
pub async fn run(bin: &str, args: &[String]) -> Result<(), AppError> {
    let output = Command::new(bin)
        .args(args)
        .output()
        .await
        .map_err(|e| AppError::Ffmpeg(format!("spawn `{bin}`: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(AppError::Ffmpeg(format!(
            "`{bin}` exited {}: {}",
            output.status,
            stderr.trim()
        )));
    }
    Ok(())
}

/// Decode-timestamps (seconds) of every **keyframe** in the source's first video
/// stream, in order. This is the raw material V1 turns into a chunk plan: you may
/// only cut a source into independently-transcodable chunks *at* these points.
///
/// Uses `ffprobe -skip_frame nokey` so only keyframes are reported.
pub async fn probe_keyframes(ffprobe: &str, source: &str) -> Result<Vec<f64>, AppError> {
    let args: [String; 11] = [
        "-loglevel",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=print_section=0",
        source,
    ]
    .map(String::from);

    let output = Command::new(ffprobe)
        .args(args)
        .output()
        .await
        .map_err(|e| AppError::Ffmpeg(format!("spawn `{ffprobe}`: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(AppError::Ffmpeg(format!(
            "ffprobe (keyframes) exited {}: {}",
            output.status,
            stderr.trim()
        )));
    }

    let text = String::from_utf8_lossy(&output.stdout);
    let mut times = Vec::new();
    for line in text.lines() {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        if let Ok(t) = s.parse::<f64>() {
            times.push(t);
        }
    }
    Ok(times)
}

/// Total duration (seconds) of the source container. The last chunk runs from the
/// final usable keyframe to here.
pub async fn probe_duration(ffprobe: &str, source: &str) -> Result<f64, AppError> {
    let args: [String; 9] = [
        "-loglevel",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=print_section=0",
        "-i",
        source,
        "-hide_banner",
    ]
    .map(String::from);

    let output = Command::new(ffprobe)
        .args(args)
        .output()
        .await
        .map_err(|e| AppError::Ffmpeg(format!("spawn `{ffprobe}`: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(AppError::Ffmpeg(format!(
            "ffprobe (duration) exited {}: {}",
            output.status,
            stderr.trim()
        )));
    }

    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse::<f64>()
        .map_err(|e| AppError::Ffmpeg(format!("could not parse duration: {e}")))
}

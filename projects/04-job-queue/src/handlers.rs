//! The dispatch layer: turn a stored job's `(kind, payload)` into a typed
//! [`JobKind`] and route it to a handler.
//!
//! This lives *outside* the queue core on purpose. [`crate::queue`] and
//! [`crate::job`] stay generic — to them a job is an opaque `kind: String` plus a
//! JSON `payload`, and the claim/lease/retry/schedule mechanics (V1–V4) never care
//! what a job *does*. The closed catalogue of things a worker knows how to run
//! lives here, so adding a job type is a change to the app layer, not to the
//! queue's row type. The queue never trusts `kind` to be anything more than a
//! routing key (SPEC security note).
//!
//! # Security — the [`JobKind::Exec`] / [`JobKind::Shell`] kinds are RCE *by design*
//!
//! A job that runs a program (or a `sh -c` line) whose contents came from a
//! `POST /jobs` body turns the enqueue endpoint into arbitrary code execution on
//! every worker. That is legitimate for a CI-runner / task-runner, but it means:
//! - the enqueue API **must** be authenticated (SPEC security checklist) — an open
//!   `POST /jobs` is now an open root shell, not just "make my workers busy";
//! - payloads must never be logged blindly (an arg or env var may be a secret);
//! - in anything real you'd want an allow-list of programs and to run the child
//!   sandboxed / unprivileged / resource-capped — a raw `sh -c` is the maximal
//!   surface. Here it's deliberately plain so the *queue* behaviour is what's on show.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter};
use tokio::process::{ChildStderr, ChildStdout, Command};

use crate::job::Job;

/// Wall-clock cap applied to an exec/shell job when its payload omits `timeout_secs`.
///
/// A command that runs longer than the worker's visibility timeout is still
/// `running` when its lease expires, so the reaper returns the job to `ready` and
/// a second worker starts a concurrent copy. Keep this comfortably under
/// `VISIBILITY_TIMEOUT_SECS`.
const DEFAULT_EXEC_TIMEOUT: Duration = Duration::from_secs(20);

/// Max bytes of the stderr tail folded into the `Err` / `last_error` message.
const MAX_STDERR: usize = 2000;
/// Cap on how many output lines a single job may write to its log file. Past this
/// we stop *writing* (but keep draining the pipe, or the child blocks on a full
/// buffer) — a runaway job can't flood disk.
const MAX_LOG_LINES: usize = 1000;
/// How many trailing stderr lines to keep for the failure message.
const STDERR_TAIL_LINES: usize = 20;
/// Default base directory for per-attempt job output files; override with `JOB_LOG_DIR`.
const DEFAULT_JOB_LOG_DIR: &str = "logs";

/// The closed catalogue of jobs a worker knows how to run.
///
/// Adjacently tagged: serde reads the stored `kind` text column as the tag and the
/// stored `payload` JSON as the variant's fields (see [`JobKind::from_job`]), so
/// `{"kind":"sleep","payload":{"ms":100}}` deserializes to [`JobKind::Sleep`].
/// Unit variants (`Noop`, `Fail`) carry no payload — a `null` payload is fine.
///
/// To teach the workers a new job type: add a variant here and an arm in
/// [`dispatch`]. `rename_all = "snake_case"` maps the Rust name to the string a
/// caller enqueues (`FlakyThenOk` ⇄ `"flaky_then_ok"`).
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", content = "payload", rename_all = "snake_case")]
pub enum JobKind {
    /// Succeed immediately; useful for enqueue/claim plumbing tests.
    Noop,
    /// Sleep for `ms` milliseconds then succeed.
    Sleep { ms: u64 },
    /// Print `msg` to stdout then succeed.
    Echo { msg: String },
    /// Always fail — a poison message that exercises retry / DLQ (V3).
    Fail,
    /// Fail while `job.attempts <= fail_n`, then succeed (flaky-downstream tests).
    FlakyThenOk { fail_n: i32 },
    /// HTTP POST to `url`; success iff the response status is 2xx.
    Webhook { url: String },
    /// Spawn `program` with `args`, capture output to a per-attempt log file.
    ///
    /// # Security
    ///
    /// See the [module-level security note](self).
    Exec {
        program: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        timeout_secs: Option<u64>,
    },
    /// Run `script` via `sh -c`, with the same capture / timeout behaviour as [`JobKind::Exec`].
    ///
    /// # Security
    ///
    /// See the [module-level security note](self).
    Shell {
        script: String,
        #[serde(default)]
        timeout_secs: Option<u64>,
    },
}

impl JobKind {
    /// Rebuild the typed job from its two stored columns.
    ///
    /// Returns `Err(reason)` — recorded as `last_error` and driven through the
    /// retry/DLQ path — when `kind` is unknown or `payload` doesn't match the
    /// variant's shape. That validation is deliberate: an unrecognised `kind` is a
    /// bad enqueue, not something to run.
    ///
    /// # Errors
    ///
    /// Returns a human-readable reason when serde cannot decode the adjacently
    /// tagged `{kind, payload}` object into a [`JobKind`] variant.
    pub fn from_job(job: &Job) -> Result<Self, String> {
        let tagged = serde_json::json!({ "kind": job.kind, "payload": job.payload });
        serde_json::from_value(tagged).map_err(|e| format!("unroutable job: {e}"))
    }
}

/// Buffered writer for one job attempt's stdout/stderr capture file.
///
/// Streams both pipes concurrently into `{JOB_LOG_DIR}/{id}/{attempts}.log`,
/// capped at [`MAX_LOG_LINES`], and keeps a rolling stderr tail for failure
/// messages. Bulk command output stays out of the operational tracing stream.
struct JobLogger {
    log: BufWriter<tokio::fs::File>,
    written: usize,
}

impl JobLogger {
    /// Create (and parent-mkdir) the log file at `log_path`.
    ///
    /// # Errors
    ///
    /// Returns a string describing mkdir or create failures.
    async fn init(log_path: PathBuf) -> Result<Self, String> {
        use tokio::fs as tfs;

        if let Some(parent_dir) = log_path.parent() {
            tfs::create_dir_all(parent_dir)
                .await
                .map_err(|e| format!("log dir {}: {e}", parent_dir.display()))?;
        }
        let log_file = tfs::File::create(&log_path)
            .await
            .map_err(|e| format!("log file {}: {e}", log_path.display()))?;
        Ok(Self {
            log: BufWriter::new(log_file),
            written: 0,
        })
    }

    /// Append one tagged line, or a single truncation marker once the cap is hit.
    ///
    /// Past [`MAX_LOG_LINES`] this is a no-op so callers can keep draining the
    /// child's pipes without filling the disk.
    ///
    /// # Errors
    ///
    /// Propagates I/O errors from the underlying writer.
    async fn write_line(&mut self, stream: &str, line: &str) -> Result<(), String> {
        let content = if self.written < MAX_LOG_LINES {
            format!("[{stream}] {line}\n")
        } else if self.written == MAX_LOG_LINES {
            format!("… truncated at {MAX_LOG_LINES} lines\n")
        } else {
            return Ok(());
        };
        self.log
            .write_all(content.as_bytes())
            .await
            .map_err(|e| e.to_string())?;
        self.written += 1;
        Ok(())
    }

    /// Drain stdout and stderr concurrently until both hit EOF, then flush the log.
    ///
    /// Returns `(stderr_tail, lines_written)`. The tail is the last
    /// [`STDERR_TAIL_LINES`] stderr lines, used to build `last_error` on failure.
    ///
    /// # Errors
    ///
    /// Returns I/O errors from reading either stream or flushing the log file.
    ///
    /// # Panics
    ///
    /// Does not panic itself; callers that take stdout/stderr from a spawned child
    /// use `expect("… piped")` before invoking this.
    async fn capture(
        mut self,
        out_stream: ChildStdout,
        err_stream: ChildStderr,
    ) -> Result<(Vec<String>, usize), String> {
        let mut out = BufReader::new(out_stream).lines();
        let mut err = BufReader::new(err_stream).lines();
        let mut tail: VecDeque<String> = VecDeque::with_capacity(STDERR_TAIL_LINES);
        let (mut out_open, mut err_open) = (true, true);

        while out_open || err_open {
            tokio::select! {
                line = out.next_line(), if out_open => match line.map_err(|e| e.to_string())? {
                    Some(l) => self.write_line("out", &l).await?,
                    None => out_open = false,
                },
                line = err.next_line(), if err_open => match line.map_err(|e| e.to_string())? {
                    Some(l) => {
                        self.write_line("err", &l).await?;
                        if tail.len() == STDERR_TAIL_LINES {
                            tail.pop_front();
                        }
                        tail.push_back(l);
                    }
                    None => err_open = false,
                },
            }
        }

        self.log.flush().await.map_err(|e| e.to_string())?;
        Ok((Vec::from(tail), self.written))
    }

    /// Path for this attempt: `{JOB_LOG_DIR|logs}/{job.id}/{job.attempts}.log`.
    fn job_log_path(job: &Job) -> PathBuf {
        let base = std::env::var("JOB_LOG_DIR").unwrap_or_else(|_| DEFAULT_JOB_LOG_DIR.to_string());
        PathBuf::from(base)
            .join(job.id.to_string())
            .join(format!("{}.log", job.attempts))
    }
}

/// Spawn `cmd`, stream both pipes into `log_path`, enforce `timeout`, and map the
/// outcome onto the job contract: exit `0` → `Ok(())`; non-zero exit, spawn
/// failure, or timeout → `Err(reason)` (recorded as `last_error`, driven through
/// retry/DLQ).
///
/// Two choices tie back to the verticals:
/// - `kill_on_drop(true)` + the timeout: a hung command is killed rather than left
///   to outlive its lease (V2).
/// - non-zero exit → `Err`: failures flow into backoff + DLQ (V3), but retrying is
///   only safe if the command is **idempotent** (at-least-once may run it 2+ times).
///
/// # Errors
///
/// Returns a string for spawn failure, capture I/O errors, wall-clock timeout, or
/// a non-zero exit (including a truncated stderr tail).
///
/// # Panics
///
/// Panics if the spawned child's stdout/stderr handles are missing despite
/// requesting piped stdio (`expect("stdout piped")` / `expect("stderr piped")`).
async fn run_process(mut cmd: Command, timeout: Duration, log_path: &Path) -> Result<(), String> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let logger = JobLogger::init(log_path.to_path_buf()).await?;

    let mut child = cmd.spawn().map_err(|e| format!("spawn failed: {e}"))?;

    let capture = async move {
        let stdout = child.stdout.take().expect("stdout piped");
        let stderr = child.stderr.take().expect("stderr piped");
        let (tail, written) = logger.capture(stdout, stderr).await?;
        let status = child.wait().await.map_err(|e| e.to_string())?;
        Ok::<_, String>((status, tail, written))
    };

    let (status, tail, written) = match tokio::time::timeout(timeout, capture).await {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => return Err(format!("process error: {e}")),
        Err(_) => return Err(format!("timed out after {}s", timeout.as_secs())),
    };

    tracing::info!(log = %log_path.display(), lines = written, "job output captured");

    if status.success() {
        return Ok(());
    }

    let err_msg = {
        let mut msg = tail.join("\n");
        if msg.len() > MAX_STDERR {
            let mut end = MAX_STDERR;
            while !msg.is_char_boundary(end) {
                end -= 1;
            }
            msg.truncate(end);
            msg.push_str("…(truncated)");
        }
        msg
    };

    let code = status
        .code()
        .map(|c| c.to_string())
        .unwrap_or_else(|| "signal".into());
    Err(format!("exit {code}: {}", err_msg.trim()))
}

/// Run one job to completion by decoding its [`JobKind`] and executing the matching arm.
///
/// `Ok(())` acks the job (→ `done`); `Err(reason)` drives the retry/DLQ path with
/// `reason` recorded as `last_error`. Called from the worker's `process_one`.
///
/// # Errors
///
/// Returns a string when [`JobKind::from_job`] fails, a handler fails (e.g.
/// [`JobKind::Fail`], webhook non-2xx), or [`run_process`] reports spawn / timeout /
/// non-zero exit.
pub async fn dispatch(job: &Job) -> Result<(), String> {
    match JobKind::from_job(job)? {
        JobKind::Noop => Ok(()),
        JobKind::Sleep { ms } => {
            tokio::time::sleep(Duration::from_millis(ms)).await;
            Ok(())
        }
        JobKind::Echo { msg } => {
            println!("echo: {}", msg);
            Ok(())
        }
        JobKind::Fail => Err("poison".to_string()),
        JobKind::FlakyThenOk { fail_n } => {
            if job.attempts <= fail_n {
                Err("poison".to_string())
            } else {
                Ok(())
            }
        }
        JobKind::Exec {
            program,
            args,
            timeout_secs,
        } => {
            let mut cmd = Command::new(program);
            cmd.args(args);
            let timeout = timeout_secs
                .map(Duration::from_secs)
                .unwrap_or(DEFAULT_EXEC_TIMEOUT);
            run_process(cmd, timeout, &JobLogger::job_log_path(job)).await
        }
        JobKind::Shell {
            script,
            timeout_secs,
        } => {
            let mut cmd = Command::new("sh");
            cmd.arg("-c").arg(script);
            let timeout = timeout_secs
                .map(Duration::from_secs)
                .unwrap_or(DEFAULT_EXEC_TIMEOUT);
            run_process(cmd, timeout, &JobLogger::job_log_path(job)).await
        }
        JobKind::Webhook { url } => {
            let client = reqwest::Client::new();
            let response = client.post(url).send().await.map_err(|e| e.to_string())?;
            if response.status().is_success() {
                Ok(())
            } else {
                Err("webhook failed".to_string())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::job::{Job, JobState};
    use chrono::Utc;

    /// A minimal `Job` for exercising decode/dispatch without a database.
    fn job(kind: &str, payload: serde_json::Value) -> Job {
        Job {
            id: 1,
            queue: "default".into(),
            kind: kind.into(),
            payload,
            state: JobState::Running,
            attempts: 0,
            max_attempts: 5,
            run_at: Utc::now(),
            locked_until: None,
            last_error: None,
            created_at: Utc::now(),
        }
    }

    fn tmp_log(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("jq-test-{tag}-{}.log", std::process::id()))
    }

    #[test]
    fn decodes_unit_variant_from_null_payload() {
        let decoded = JobKind::from_job(&job("noop", serde_json::Value::Null)).unwrap();
        assert!(matches!(decoded, JobKind::Noop));
    }

    #[test]
    fn decodes_data_variant() {
        let decoded = JobKind::from_job(&job("sleep", serde_json::json!({"ms": 50}))).unwrap();
        assert!(matches!(decoded, JobKind::Sleep { ms: 50 }));
    }

    #[test]
    fn decodes_exec_with_defaulted_fields() {
        match JobKind::from_job(&job("exec", serde_json::json!({"program": "echo"}))).unwrap() {
            JobKind::Exec {
                program,
                args,
                timeout_secs,
            } => {
                assert_eq!(program, "echo");
                assert!(args.is_empty());
                assert_eq!(timeout_secs, None);
            }
            other => panic!("wrong variant: {other:?}"),
        }
    }

    #[test]
    fn rejects_unknown_kind() {
        assert!(JobKind::from_job(&job("teleport", serde_json::Value::Null)).is_err());
    }

    #[test]
    fn rejects_malformed_payload() {
        assert!(JobKind::from_job(&job("sleep", serde_json::json!({"ms": "soon"}))).is_err());
    }

    #[tokio::test]
    async fn dispatch_noop_ok_and_fail_err() {
        assert!(dispatch(&job("noop", serde_json::Value::Null))
            .await
            .is_ok());
        assert!(dispatch(&job("fail", serde_json::Value::Null))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn flaky_fails_until_attempts_exceed_fail_n() {
        let mut j = job("flaky_then_ok", serde_json::json!({"fail_n": 2}));
        j.attempts = 2;
        assert!(
            dispatch(&j).await.is_err(),
            "attempts <= fail_n should fail"
        );
        j.attempts = 3;
        assert!(
            dispatch(&j).await.is_ok(),
            "attempts > fail_n should succeed"
        );
    }

    #[tokio::test]
    async fn writes_both_streams_to_the_log_file() {
        let log = tmp_log("streams");
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg("echo hi-out; echo hi-err >&2");
        run_process(cmd, Duration::from_secs(5), &log)
            .await
            .unwrap();

        let body = std::fs::read_to_string(&log).unwrap();
        assert!(body.contains("[out] hi-out"), "body: {body}");
        assert!(body.contains("[err] hi-err"), "body: {body}");
        let _ = std::fs::remove_file(&log);
    }

    #[tokio::test]
    async fn nonzero_exit_reports_code_and_stderr_tail() {
        let log = tmp_log("fail");
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg("echo boom >&2; exit 3");
        let err = run_process(cmd, Duration::from_secs(5), &log)
            .await
            .unwrap_err();
        assert!(err.contains("exit 3"), "got: {err}");
        assert!(err.contains("boom"), "stderr tail missing: {err}");
        let _ = std::fs::remove_file(&log);
    }

    #[tokio::test]
    async fn hung_command_times_out() {
        let log = tmp_log("hang");
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg("sleep 10");
        let err = run_process(cmd, Duration::from_millis(200), &log)
            .await
            .unwrap_err();
        assert!(err.contains("timed out"), "got: {err}");
        let _ = std::fs::remove_file(&log);
    }

    #[tokio::test]
    async fn output_is_capped_with_a_truncation_marker() {
        let log = tmp_log("cap");
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg(format!("seq 1 {}", MAX_LOG_LINES + 50));
        run_process(cmd, Duration::from_secs(30), &log)
            .await
            .unwrap();

        let body = std::fs::read_to_string(&log).unwrap();
        assert!(body.contains("truncated at"), "expected truncation marker");
        let _ = std::fs::remove_file(&log);
    }

    #[test]
    fn log_path_is_keyed_by_id_and_attempt() {
        let mut j = job("exec", serde_json::json!({"program": "echo"}));
        j.id = 42;
        j.attempts = 3;
        let path = JobLogger::job_log_path(&j);
        assert!(path.ends_with("42/3.log"), "path: {}", path.display());
    }
}

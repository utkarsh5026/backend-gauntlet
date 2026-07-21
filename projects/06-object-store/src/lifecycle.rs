//! Lifecycle & tiering — objects age out, or migrate to a cheaper cold tier.
//!
//! Two operations hide in the SPEC's one line, and they share almost nothing:
//!
//!   1. **Expiration** — after a configured age, the object *disappears*. This is
//!      cheap: append a delete marker (or drop the index row) and let V3's GC
//!      reclaim the now-unreferenced blob. No new storage, no decode path.
//!   2. **Tiering** — after a configured age, the object *stays retrievable* but
//!      its bytes move to a compressed cold representation, so GET must decode
//!      transparently. This is the hard half, and everything below is about it.
//!
//! ## The CAS collision (read this before touching `tier_blob`)
//!
//! A blob is named `objects/<sha256-of-its-bytes>`. Compress the bytes and the
//! hash changes — so tiering is a fork the design doc must defend:
//!   - *compress-then-hash*: cold file named by the compressed hash → breaks
//!     dedup and orphans every index entry pointing at the old digest. **No.**
//!   - *hash-then-compress*: identity stays the **plaintext** digest; the digest,
//!     ETag, and dedup key never move. Compression is a *physical encoding of a
//!     blob*, not a new identity. **This is the one we build.**
//!
//! So the mental model: a blob has a fixed *logical digest* ([`Digest`]) and a
//! *physical representation* ([`Physical`]) that tiering flips between
//! `objects/<h>` ([`Encoding::Raw`]) and `cold/<h>.zst` ([`Encoding::Zstd`]).
//!
//! ## The subtlety that bites: decisions are per-object, transforms are per-blob
//!
//! `last_modified` lives on the *version* ([`crate::object::VersionEntry`]), but
//! a blob is **shared** across keys by dedup. A blob may back a 90-day-old key
//! *and* one you PUT this morning. So a blob is cold-eligible only when its
//! **youngest referrer** is older than `tier_after` — otherwise you'd freeze a
//! hot object. That's a `max(last_modified)` over the blob's referrers, computed
//! the same way V3's GC mark phase already discovers who references what.
//!
//! ## Wiring
//!
//! `Lifecycle::spawn` is started once from `main.rs` after `AppState::open`
//! (hold the returned handle for the process lifetime). The tiering-aware read
//! path ([`Lifecycle::open_tiered`]) must replace the raw `store.open_blob` call
//! in the GET handler so cold objects decode transparently.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use async_compression::tokio::bufread::ZstdDecoder;
use async_compression::tokio::write::ZstdEncoder;
use chrono::{DateTime, Utc};
use futures_util::{stream, StreamExt, TryStreamExt};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncRead, AsyncWriteExt, BufReader};
use tracing::{error, info};

use crate::durable::{publish_temp, TempEntry};
use crate::error::AppError;
use crate::index_backend::IndexBackend;
use crate::naming::{Bucket, Key};
use crate::object::{Digest, ObjectRef};
use crate::store::Store;

/// The user-declared policy for one bucket — the *rules*, stored durably in that
/// bucket's `metadata.json`. Distinct from the sweeper's *cadence* (the
/// `scan_interval` passed to [`Lifecycle::spawn`]); this is *what to do* when it
/// sweeps, and it belongs to the bucket owner.
///
/// A policy is a **list** of rules (S3 semantics): the sweep finds the rule whose
/// filter matches a key, then applies its actions. Empty list ⇒ nothing ages.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LifecyclePolicy {
    /// Ordered rules; the first enabled match wins (see [`Self::matching_rule`]).
    #[serde(default)]
    pub rules: Vec<LifecycleRule>,
}

/// One lifecycle rule: a **filter** (which keys) plus up to four **actions**
/// (what happens as they age). Every action is optional and independent, so a
/// single rule can, e.g., cool at 365 d and delete at 1825 d.
///
/// Ages are in **days from `last_modified`** — the same clock
/// [`Lifecycle::older_than_days`] uses. (S3 also allows absolute dates; skipped
/// until something needs one.)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LifecycleRule {
    /// Human name for the rule — lets an operator identify it in logs/UI.
    #[serde(default)]
    pub id: String,

    /// A disabled rule is kept on disk but skipped by the sweep — pause without
    /// losing the definition. Defaults to `true` so an omitted field means "on".
    #[serde(default = "default_true")]
    pub enabled: bool,

    /// Key-prefix filter. `None` ⇒ the whole bucket. A key matches when it
    /// `starts_with` this prefix.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prefix: Option<String>,

    /// Migrate the *current* object's blob to the cold tier after this age.
    /// Must be less than `expire_after_days` when both are set.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tier_after_days: Option<u32>,

    /// Delete the *current* object (append a delete marker) after this age.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expire_after_days: Option<u32>,

    /// Reap *noncurrent* versions (superseded by a newer overwrite) after this
    /// age. Distinct from `expire_after_days` because in a versioned bucket the
    /// real disk hog is stale history, not the live object. `None` ⇒ keep all
    /// history. *(natural once V1 versioning is in — see module docs)*
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub noncurrent_expire_after_days: Option<u32>,

    /// Abort multipart uploads whose parts have sat in staging longer than this,
    /// reclaiming leaked part bytes. `None` ⇒ never auto-abort.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub abort_multipart_after_days: Option<u32>,
}

fn default_true() -> bool {
    true
}

impl LifecyclePolicy {
    /// The first enabled rule whose filter matches `key`, or `None` if none do.
    ///
    /// S3 evaluates rules in order and the first match wins; keep that so a
    /// specific `logs/` rule can precede a catch-all. Skips disabled rules.
    pub fn matching_rule(&self, key: &str) -> Option<&LifecycleRule> {
        self.rules.iter().find(|rule| {
            rule.enabled
                && match &rule.prefix {
                    None => true,
                    Some(prefix) => key.starts_with(prefix),
                }
        })
    }

    /// Reject a policy that can't be enforced coherently before it's persisted:
    /// e.g. `tier_after_days >= expire_after_days` (you'd cool a thing you're
    /// about to delete), or a zero age. Called on the PUT-policy path.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::InvalidRequest`] when any age field is `Some(0)`, or
    /// when both tier and expire ages are set and `tier_after_days >= expire_after_days`.
    pub fn validate(&self) -> Result<(), AppError> {
        for rule in &self.rules {
            for (name, days) in [
                ("tier_after_days", rule.tier_after_days),
                ("expire_after_days", rule.expire_after_days),
                (
                    "noncurrent_expire_after_days",
                    rule.noncurrent_expire_after_days,
                ),
                (
                    "abort_multipart_after_days",
                    rule.abort_multipart_after_days,
                ),
            ] {
                if days == Some(0) {
                    return Err(AppError::InvalidRequest(format!(
                        "{name} must be greater than zero"
                    )));
                }
            }

            if let (Some(tier), Some(expire)) = (rule.tier_after_days, rule.expire_after_days) {
                if tier >= expire {
                    return Err(AppError::InvalidRequest(
                        "tier_after_days must be less than expire_after_days".into(),
                    ));
                }
            }
        }
        Ok(())
    }
}

/// On-disk encoding of a blob's bytes. The *logical* digest is unchanged by
/// this — it always names the plaintext.
pub enum Encoding {
    /// Plaintext, in the hot `objects/` tree. Supports O(1) ranged reads.
    Raw,
    /// Zstd-framed, in the cold tier. Ranged reads must decode from a frame
    /// boundary — the cold-tier latency tradeoff, stated in the design doc.
    Zstd,
}

/// Where a blob physically lives and how it's encoded. The read path resolves
/// this before opening, so callers never learn which tier served them.
pub struct Physical {
    /// Absolute path of the file that holds the blob's bytes.
    pub path: PathBuf,
    /// How those bytes are encoded on disk ([`Encoding::Raw`] or [`Encoding::Zstd`]).
    pub encoding: Encoding,
}

/// What one sweep did — logged and surfaced as metrics, never used for control
/// flow (the next sweep re-derives everything from disk).
#[derive(Default)]
pub struct SweepReport {
    /// Live objects expired this pass (delete markers appended).
    pub expired: u64,
    /// Blobs newly migrated to the cold tier this pass.
    pub tiered: u64,
    /// Sum of `(hot_size - cold_size)` over successful tiers — bytes saved on disk.
    pub bytes_reclaimed: u64,
}

/// The lifecycle engine: owns the age policy and the crash-safe migration dance
/// over V1's [`Store`] and V3's [`IndexBackend`].
pub struct Lifecycle {
    index: Arc<IndexBackend>,
    store: Arc<Store>,
}

impl Lifecycle {
    /// Bound parallel expire/tier I/O so a large bucket doesn't thrash the disk.
    const SWEEP_CONCURRENCY: usize = 32;

    /// Build the engine. Cheap — the actual scanning happens in [`Self::spawn`].
    pub fn new(index: Arc<IndexBackend>, store: Arc<Store>) -> Arc<Self> {
        Arc::new(Self { index, store })
    }

    /// Start the background sweep loop and hand back its task handle.
    ///
    /// The loop is a `tokio::time::interval` at `scan_interval` that calls
    /// [`Self::run_once`] each tick, logging its [`SweepReport`]. Keep the handle
    /// alive for the process's lifetime; dropping it aborts the sweeper.
    pub fn spawn(self: Arc<Self>, scan_interval: Duration) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(scan_interval);
            loop {
                interval.tick().await;
                let report = self.run_once().await;
                if let Ok(report) = report {
                    info!(
                        "Lifecycle sweep report: expired={}, tiered={}, bytes_reclaimed={}",
                        report.expired, report.tiered, report.bytes_reclaimed
                    );
                } else {
                    error!("Lifecycle sweep failed: {:?}", report.err());
                }
            }
        })
    }

    /// One full pass: expire aged objects, then cool eligible blobs.
    ///
    /// Idempotent and stateless across runs — everything is re-derived from the
    /// index + store, so a crash mid-sweep just means the next tick finishes the
    /// job. Order matters: expire first (it drops referrers, which may make more
    /// blobs cold-eligible or fully unreferenced), then tier.
    ///
    /// # Errors
    ///
    /// Propagates index/store I/O failures from listing buckets, applying deletes,
    /// or migrating blobs. See [`Self::run_once_at`].
    pub async fn run_once(&self) -> Result<SweepReport, AppError> {
        self.run_once_at(Utc::now()).await
    }

    /// [`run_once`](Self::run_once) with the sweep instant injected.
    ///
    /// The production loop passes `Utc::now()`; tests pass any instant so a
    /// freshly-written object can be swept "in the future" without backdating it
    /// on disk or sleeping. Every age decision in the pass is measured against
    /// this single `now`, so a long sweep judges every object consistently.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] if listing buckets/entries, loading bucket metadata,
    /// deleting an expired key, or tiering a blob fails.
    pub async fn run_once_at(&self, now: DateTime<Utc>) -> Result<SweepReport, AppError> {
        let mut report = SweepReport::default();

        for bucket in self.index.buckets().await? {
            let bucket = Bucket::from_trusted(bucket);
            let meta = self.index.load_bucket_metadata(&bucket).await?;
            let policy = &meta.lifecycle;
            let entries = self.index.index_entries(&bucket).await?;

            // Live keys that match a lifecycle rule. Lazy — re-walks `entries`
            // each call; no intermediate Vec of triples.
            let ruled_live = || {
                entries.iter().filter_map(|entry| {
                    let rule = policy.matching_rule(&entry.key)?;
                    let live = entry.latest_live()?;
                    Some((&entry.key, rule, live))
                })
            };

            // Keys whose latest live version matches an expire rule.
            let to_expire: HashSet<Key> = ruled_live()
                .filter(|(_, rule, live)| {
                    rule.expire_after_days
                        .is_some_and(|days| Self::older_than_days(live.last_modified, now, days))
                })
                .map(|(key, _, _)| key.clone())
                .collect();

            // All surviving live keys — including ones with *no* rule — still
            // pin digests hot, so this walk is wider than `ruled_live`.
            let mut youngest: HashMap<Digest, DateTime<Utc>> = HashMap::new();
            for entry in &entries {
                if to_expire.contains(&entry.key) {
                    continue;
                }
                if let Some(live) = entry.latest_live() {
                    youngest
                        .entry(live.digest)
                        .and_modify(|t| *t = (*t).max(live.last_modified))
                        .or_insert(live.last_modified);
                }
            }

            let to_tier: HashSet<Digest> = ruled_live()
                .filter(|(key, _, _)| !to_expire.contains(*key))
                .filter_map(|(_, rule, live)| {
                    let days = rule.tier_after_days?;
                    let &lm = youngest.get(&live.digest)?;
                    Self::older_than_days(lm, now, days).then_some(live.digest)
                })
                .collect();

            let expired = to_expire.len() as u64;
            stream::iter(to_expire.iter())
                .map(Ok)
                .try_for_each_concurrent(Self::SWEEP_CONCURRENCY, |key| async {
                    self.index.delete(&bucket, key, ObjectRef::Latest).await
                })
                .await?;
            report.expired += expired;

            // Each task returns stats instead of mutating `report` (which would
            // require FnMut and can't run concurrently).
            let tier_outcomes: Vec<(u64, u64)> = stream::iter(to_tier)
                .map(|digest| async move {
                    match self.locate(&digest).await {
                        Ok(physical) if matches!(physical.encoding, Encoding::Raw) => {}
                        Ok(_) | Err(AppError::NoSuchKey) => return Ok((0, 0)),
                        Err(err) => return Err(err),
                    }
                    let hot_size = tokio::fs::metadata(self.store.blob_path(&digest))
                        .await?
                        .len();
                    self.tier_blob(&digest).await?;
                    let cold_size = tokio::fs::metadata(self.cold_path(&digest)).await?.len();
                    Ok((1u64, hot_size.saturating_sub(cold_size)))
                })
                .buffer_unordered(Self::SWEEP_CONCURRENCY)
                .try_collect()
                .await?;

            for (n, bytes) in tier_outcomes {
                report.tiered += n;
                report.bytes_reclaimed += bytes;
            }
        }

        Ok(report)
    }

    /// Migrate one blob from the hot tree to the compressed cold tier — the
    /// crash-safe half of tiering. Mirrors V1's PUT durability dance:
    ///
    ///   1. stream `objects/<h>` through a zstd encoder into `cold/<h>.zst.tmp`
    ///   2. fsync the temp, `rename` it onto `cold/<h>.zst`, fsync the cold dir
    ///   3. flip this blob's [`Physical`] descriptor to [`Encoding::Zstd`]
    ///   4. only *now* unlink the hot `objects/<h>`
    ///
    /// A crash before step 3 leaves a stray cold temp (reaped like any orphan);
    /// a crash before step 4 leaves both copies (harmless — reads still work,
    /// next sweep finishes it). Never delete the hot copy before the cold copy
    /// is durable and the descriptor committed.
    ///
    /// Idempotent: if `cold/<h>.zst` already exists, any leftover hot copy is
    /// unlinked and the call returns `Ok(())`.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] on filesystem or store failures while opening the
    /// hot blob, writing/publishing the cold file, or unlinking the hot copy.
    ///
    /// # Panics
    ///
    /// Panics if the computed cold path somehow has no parent (it always does
    /// for a well-formed data dir).
    pub async fn tier_blob(&self, digest: &Digest) -> Result<(), AppError> {
        let cold = self.cold_path(digest);
        if tokio::fs::try_exists(&cold).await? {
            if self.store.contains(digest).await {
                self.store.remove(digest).await?;
            }
            return Ok(());
        }

        let mut hot = self.store.open_blob(digest).await?;
        let parent = cold.parent().expect("cold path has a parent");
        tokio::fs::create_dir_all(parent).await?;

        let mut temp = TempEntry::unique_in(parent, &format!("{}.zst.tmp", digest.as_str()));

        {
            let file = tokio::fs::File::create(temp.path()).await?;
            let mut encoder = ZstdEncoder::new(file);
            tokio::io::copy(&mut hot, &mut encoder).await?;
            encoder.shutdown().await?;
            let file = encoder.into_inner();
            file.sync_all().await?;
        }

        publish_temp(temp.path(), &cold).await?;
        temp.disarm();
        self.store.remove(digest).await?;
        Ok(())
    }

    /// Resolve where a blob physically lives + how it's encoded.
    ///
    /// Cheapest impl: probe `objects/<h>` then `cold/<h>.zst`. Cleaner: read a
    /// small descriptor the migrator writes. Either way, GET calls this instead
    /// of assuming the hot path.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] when neither the hot nor cold path exists.
    /// Other filesystem probe failures surface as [`AppError`] as well.
    pub async fn locate(&self, digest: &Digest) -> Result<Physical, AppError> {
        let hot = self.store.blob_path(digest);
        if tokio::fs::try_exists(&hot).await? {
            return Ok(Physical {
                path: hot,
                encoding: Encoding::Raw,
            });
        }
        let cold = self.cold_path(digest);
        if tokio::fs::try_exists(&cold).await? {
            return Ok(Physical {
                path: cold,
                encoding: Encoding::Zstd,
            });
        }
        Err(AppError::NoSuchKey)
    }

    /// Open a blob for reading, transparently decoding a cold one.
    ///
    /// For [`Encoding::Raw`] this is just `store.open_blob`. For
    /// [`Encoding::Zstd`] it wraps the file in a **streaming** zstd decoder
    /// (`async-compression`'s `ZstdDecoder`) so whole objects never buffer in
    /// RAM — preserve the streaming property `streaming.rs` fought for. Returned
    /// boxed so the two arms share one type.
    ///
    /// # Errors
    ///
    /// Propagates [`Self::locate`] failures (including [`AppError::NoSuchKey`])
    /// and any I/O error opening the hot or cold file.
    pub async fn open_tiered(
        &self,
        digest: &Digest,
    ) -> Result<Pin<Box<dyn AsyncRead + Send>>, AppError> {
        let physical = self.locate(digest).await?;
        match physical.encoding {
            Encoding::Raw => {
                let file = self.store.open_blob(digest).await?;
                Ok(Box::pin(file) as Pin<Box<dyn AsyncRead + Send>>)
            }
            Encoding::Zstd => {
                let file = tokio::fs::File::open(&physical.path).await?;
                let decoder = ZstdDecoder::new(BufReader::new(file));
                Ok(Box::pin(decoder) as Pin<Box<dyn AsyncRead + Send>>)
            }
        }
    }

    /// Cold-tier path for `digest`: `<data>/cold/<ab>/<cd>/<digest>.zst`.
    ///
    /// Mirrors the hot tree's two-level shard layout so directory fan-out stays
    /// the same after a blob moves tiers. Parent of `objects_root` is the data
    /// dir; falls back to `.` if somehow rootless.
    #[inline]
    fn cold_path(&self, digest: &Digest) -> PathBuf {
        self.store
            .objects_root()
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("cold")
            .join(&digest.as_str()[0..2])
            .join(&digest.as_str()[2..4])
            .join(format!("{}.zst", digest.as_str()))
    }

    /// Return whether `last_modified` is at least `days` before `now`.
    ///
    /// Inclusive at the boundary (`>=`). A future `last_modified` (clock skew)
    /// never counts as old enough. Pure — no I/O; used by the sweep and by tests
    /// that inject a simulated `now`.
    #[inline]
    pub fn older_than_days(last_modified: DateTime<Utc>, now: DateTime<Utc>, days: u32) -> bool {
        now.signed_duration_since(last_modified) >= chrono::Duration::days(i64::from(days))
    }
}

/// Unit tests for [`LifecyclePolicy`] filter matching and validation.
#[cfg(test)]
mod policy_tests {
    use super::*;

    fn rule(id: &str) -> LifecycleRule {
        LifecycleRule {
            id: id.into(),
            enabled: true,
            prefix: None,
            tier_after_days: None,
            expire_after_days: None,
            noncurrent_expire_after_days: None,
            abort_multipart_after_days: None,
        }
    }

    fn policy(rules: Vec<LifecycleRule>) -> LifecyclePolicy {
        LifecyclePolicy { rules }
    }

    #[test]
    fn validate_accepts_an_empty_policy() {
        assert!(LifecyclePolicy::default().validate().is_ok());
    }

    #[test]
    fn validate_accepts_positive_ages_and_tier_before_expire() {
        let mut r = rule("cool-then-delete");
        r.tier_after_days = Some(30);
        r.expire_after_days = Some(365);
        r.noncurrent_expire_after_days = Some(90);
        r.abort_multipart_after_days = Some(7);
        assert!(policy(vec![r]).validate().is_ok());
    }

    #[test]
    fn validate_accepts_a_rule_with_only_one_age_set() {
        let mut tier_only = rule("tier");
        tier_only.tier_after_days = Some(30);
        let mut expire_only = rule("expire");
        expire_only.expire_after_days = Some(365);
        assert!(policy(vec![tier_only, expire_only]).validate().is_ok());
    }

    #[test]
    fn validate_rejects_tier_after_days_not_less_than_expire() {
        let mut equal = rule("equal");
        equal.tier_after_days = Some(30);
        equal.expire_after_days = Some(30);
        assert!(matches!(
            policy(vec![equal]).validate(),
            Err(AppError::InvalidRequest(_))
        ));

        let mut inverted = rule("inverted");
        inverted.tier_after_days = Some(100);
        inverted.expire_after_days = Some(10);
        assert!(matches!(
            policy(vec![inverted]).validate(),
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[test]
    fn validate_rejects_zero_for_every_age_field() {
        let cases: [fn(&mut LifecycleRule); 4] = [
            |r| r.tier_after_days = Some(0),
            |r| r.expire_after_days = Some(0),
            |r| r.noncurrent_expire_after_days = Some(0),
            |r| r.abort_multipart_after_days = Some(0),
        ];
        for set_zero in cases {
            let mut r = rule("zero");
            set_zero(&mut r);
            assert!(
                matches!(policy(vec![r]).validate(), Err(AppError::InvalidRequest(_))),
                "zero age must be rejected"
            );
        }
    }

    #[test]
    fn matching_rule_with_no_prefix_matches_any_key() {
        let p = policy(vec![rule("all")]);
        assert_eq!(p.matching_rule("anything").unwrap().id, "all");
        assert_eq!(p.matching_rule("").unwrap().id, "all");
    }

    #[test]
    fn matching_rule_matches_key_prefix_and_skips_non_matches() {
        let mut logs = rule("logs");
        logs.prefix = Some("logs/".into());
        let p = policy(vec![logs]);

        assert_eq!(p.matching_rule("logs/a.txt").unwrap().id, "logs");
        assert!(p.matching_rule("photos/a.jpg").is_none());
    }

    #[test]
    fn matching_rule_skips_disabled_rules() {
        let mut paused = rule("paused");
        paused.enabled = false;
        paused.prefix = Some("logs/".into());
        let p = policy(vec![paused]);
        assert!(p.matching_rule("logs/a.txt").is_none());
    }

    #[test]
    fn matching_rule_returns_the_first_enabled_match() {
        let mut specific = rule("specific");
        specific.prefix = Some("logs/urgent/".into());
        let mut broad = rule("broad");
        broad.prefix = Some("logs/".into());
        let p = policy(vec![specific, broad]);

        assert_eq!(p.matching_rule("logs/urgent/x").unwrap().id, "specific");
        assert_eq!(p.matching_rule("logs/other").unwrap().id, "broad");
    }

    #[test]
    fn matching_rule_is_none_when_no_rules_match() {
        assert!(LifecyclePolicy::default().matching_rule("k").is_none());
    }
}

/// The pure age predicate — the "Decide" half, with no I/O. Time is an input, so
/// every boundary is deterministic (no sleeping, no wall-clock flakiness).
#[cfg(test)]
mod age_tests {
    use super::*;

    fn days(n: i64) -> chrono::Duration {
        chrono::Duration::days(n)
    }

    #[test]
    fn true_past_the_threshold_false_within_it() {
        let now = Utc::now();
        assert!(
            Lifecycle::older_than_days(now - days(31), now, 30),
            "31d old with a 30d threshold is expired"
        );
        assert!(
            !Lifecycle::older_than_days(now - days(29), now, 30),
            "29d old with a 30d threshold is not"
        );
    }

    #[test]
    fn exactly_at_the_threshold_counts_as_old_enough() {
        // The comparison is `>=`, so the boundary is inclusive — pin it so a
        // future refactor can't silently flip `>=` to `>`.
        let now = Utc::now();
        assert!(Lifecycle::older_than_days(now - days(30), now, 30));
    }

    #[test]
    fn a_future_last_modified_is_never_old_enough() {
        // Clock skew / backdated write: `now - last_modified` is negative, which
        // must read as "not yet aged", not underflow into a huge duration.
        let now = Utc::now();
        assert!(!Lifecycle::older_than_days(now + days(5), now, 1));
    }

    #[test]
    fn is_monotonic_in_age() {
        // Older can never be *less* expired than newer at the same threshold.
        let now = Utc::now();
        for threshold in [1u32, 7, 30, 365] {
            for age in 0..40i64 {
                let older = Lifecycle::older_than_days(now - days(age + 1), now, threshold);
                let newer = Lifecycle::older_than_days(now - days(age), now, threshold);
                assert!(
                    older || !newer,
                    "threshold={threshold} age={age}: older must be >= newer in expiry"
                );
            }
        }
    }
}

/// The tiering round-trip: `tier_blob` → `locate` → `open_tiered`, over a real
/// on-disk `Store`. This is the transparency guarantee — after a blob moves to
/// the cold tier, a read still yields the exact original bytes.
#[cfg(test)]
mod tiering_tests {
    use super::*;
    use crate::index::Index;
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

    /// A blob store, index, and lifecycle engine over a throwaway data dir. The
    /// `Store` handle is returned too so tests can plant/inspect blobs directly.
    fn setup(root: &Path) -> (Arc<Lifecycle>, Arc<Store>) {
        let store = Store::open(root).expect("open store");
        let index = Index::open(root, store.clone()).expect("open index");
        let lifecycle = Lifecycle::new(Arc::new(IndexBackend::local(index)), store.clone());
        (lifecycle, store)
    }

    /// A syntactically valid 64-hex digest from a single repeated nibble.
    fn digest(nibble: char) -> Digest {
        Digest(std::iter::repeat_n(nibble, 64).collect())
    }

    /// Write `bytes` straight into the hot blob tree at the digest's path — the
    /// committed on-disk shape, without going through the whole PUT stream.
    async fn plant(store: &Store, digest: &Digest, bytes: &[u8]) {
        let path = store.blob_path(digest);
        tokio::fs::create_dir_all(path.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&path, bytes).await.unwrap();
    }

    async fn read_all(reader: &mut Pin<Box<dyn AsyncRead + Send>>) -> Vec<u8> {
        let mut buf = Vec::new();
        reader.read_to_end(&mut buf).await.expect("read decoded");
        buf
    }

    #[tokio::test]
    async fn tier_blob_moves_hot_to_cold_and_get_still_round_trips() {
        let root = TempDir::new().unwrap();
        let (lifecycle, store) = setup(root.path());
        let digest = digest('a');
        // Highly compressible so the cold copy is provably smaller.
        let payload = b"the quick brown fox jumps over the lazy dog. ".repeat(200);
        plant(&store, &digest, &payload).await;

        // Before: hot, located as Raw.
        assert!(store.contains(&digest).await);
        assert!(matches!(
            lifecycle.locate(&digest).await.unwrap().encoding,
            Encoding::Raw
        ));

        lifecycle.tier_blob(&digest).await.expect("tier");

        // After: hot copy gone, blob now located in the cold tier.
        assert!(!store.contains(&digest).await, "hot copy must be unlinked");
        let cold = lifecycle.locate(&digest).await.unwrap();
        assert!(matches!(cold.encoding, Encoding::Zstd));

        // The cold file is actually compressed (smaller than the plaintext).
        let cold_len = tokio::fs::metadata(&cold.path).await.unwrap().len();
        assert!(
            cold_len < payload.len() as u64,
            "compressible payload must shrink: {cold_len} vs {}",
            payload.len()
        );

        // Transparency: a read of the cold blob yields the exact bytes back.
        let mut reader = lifecycle.open_tiered(&digest).await.expect("open cold");
        assert_eq!(read_all(&mut reader).await, payload);
    }

    #[tokio::test]
    async fn open_tiered_reads_a_hot_blob_unchanged() {
        let root = TempDir::new().unwrap();
        let (lifecycle, store) = setup(root.path());
        let digest = digest('b');
        let payload = b"raw path: no decode, byte-for-byte".to_vec();
        plant(&store, &digest, &payload).await;

        let mut reader = lifecycle.open_tiered(&digest).await.expect("open hot");
        assert_eq!(read_all(&mut reader).await, payload);
    }

    #[tokio::test]
    async fn tier_blob_is_idempotent_when_already_cold() {
        let root = TempDir::new().unwrap();
        let (lifecycle, store) = setup(root.path());
        let digest = digest('c');
        let payload = b"tier me twice".repeat(64);
        plant(&store, &digest, &payload).await;

        lifecycle.tier_blob(&digest).await.expect("first tier");
        // A second sweep re-encountering the same digest must not error or
        // corrupt the cold copy — a crash mid-migration re-runs this path.
        lifecycle
            .tier_blob(&digest)
            .await
            .expect("second tier is a no-op");

        assert!(!store.contains(&digest).await);
        let mut reader = lifecycle
            .open_tiered(&digest)
            .await
            .expect("still readable");
        assert_eq!(read_all(&mut reader).await, payload);
    }

    #[tokio::test]
    async fn locate_is_no_such_key_for_an_unknown_digest() {
        let root = TempDir::new().unwrap();
        let (lifecycle, _store) = setup(root.path());
        assert!(matches!(
            lifecycle.locate(&digest('d')).await,
            Err(AppError::NoSuchKey)
        ));
    }
}

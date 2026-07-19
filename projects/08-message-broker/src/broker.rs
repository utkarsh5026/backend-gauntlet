//! The broker: the top-level owner that maps topic names to topics and holds the
//! consumer-group coordinator. Plumbing/wiring — the interesting behaviour lives
//! in the verticals it composes (`Topic` → V3, `Log`/`Index` → V1/V2,
//! `GroupCoordinator` → V4).
//!
//! On-disk layout under `DATA_DIR`:
//! ```text
//! <data_dir>/
//!   topics/<topic>/<partition>/{…}.log + {…}.index   ← V1 + V2
//!   groups/…                                          ← V4 committed offsets
//! ```

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::RwLock;

use crate::error::AppError;
use crate::group::GroupCoordinator;
use crate::log::LogConfig;
use crate::topic::Topic;

/// A lightweight view of a topic for the list endpoint.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TopicInfo {
    pub name: String,
    pub partitions: usize,
}

/// The broker. Cloneable via `Arc` into every request handler.
pub struct Broker {
    topics_dir: PathBuf,
    config: LogConfig,
    default_partitions: u32,
    topics: RwLock<HashMap<String, Arc<Topic>>>,
    groups: Arc<GroupCoordinator>,
}

impl Broker {
    /// Open the broker under `data_dir`, reloading any topics already on disk.
    pub fn open(
        data_dir: impl Into<PathBuf>,
        config: LogConfig,
        default_partitions: u32,
    ) -> Result<Arc<Self>, AppError> {
        let data_dir = data_dir.into();
        let topics_dir = data_dir.join("topics");
        std::fs::create_dir_all(&topics_dir)?;
        let groups = GroupCoordinator::open(data_dir.join("groups"))?;

        // Reload existing topics: each subdirectory of topics/ is a topic. Their
        // logs recover their own offsets on open (V1 recovery).
        let mut topics = HashMap::new();
        for entry in std::fs::read_dir(&topics_dir)? {
            let entry = entry?;
            if !entry.path().is_dir() {
                continue;
            }
            if let Some(name) = entry.file_name().to_str() {
                let topic = Topic::open(&topics_dir, name, config)?;
                topics.insert(name.to_string(), topic);
            }
        }

        Ok(Arc::new(Self {
            topics_dir,
            config,
            default_partitions,
            topics: RwLock::new(topics),
            groups,
        }))
    }

    /// The consumer-group coordinator (V4).
    pub fn groups(&self) -> &Arc<GroupCoordinator> {
        &self.groups
    }

    /// Create a topic. `partitions` of `None` uses the configured default.
    pub async fn create_topic(
        &self,
        name: &str,
        partitions: Option<u32>,
    ) -> Result<Arc<Topic>, AppError> {
        validate_topic_name(name)?;
        let mut topics = self.topics.write().await;
        if topics.contains_key(name) {
            return Err(AppError::TopicAlreadyExists);
        }
        let n = partitions.unwrap_or(self.default_partitions);
        let topic = Topic::create(&self.topics_dir, name, n, self.config)?;
        topics.insert(name.to_string(), topic.clone());
        Ok(topic)
    }

    /// Look up a topic by name.
    pub async fn topic(&self, name: &str) -> Result<Arc<Topic>, AppError> {
        self.topics
            .read()
            .await
            .get(name)
            .cloned()
            .ok_or(AppError::UnknownTopic)
    }

    /// List all topics (for `GET /topics`).
    pub async fn list_topics(&self) -> Vec<TopicInfo> {
        self.topics
            .read()
            .await
            .values()
            .map(|t| TopicInfo {
                name: t.name().to_string(),
                partitions: t.partition_count(),
            })
            .collect()
    }
}

/// Reject topic names that would escape the data dir or make illegal paths — the
/// name becomes a directory (see the security horizontal item).
fn validate_topic_name(name: &str) -> Result<(), AppError> {
    let ok = !name.is_empty()
        && name.len() <= 255
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        && name != "."
        && name != "..";
    if ok {
        Ok(())
    } else {
        Err(AppError::InvalidRequest(format!(
            "illegal topic name: {name:?}"
        )))
    }
}

//! The media library + the packaging pipeline wiring.
//!
//! This is **plumbing** — it is fully implemented. It scans `MEDIA_DIR` at startup
//! into an in-memory catalog of assets → renditions → source files, and it composes
//! the vertical building blocks into the outputs the HTTP layer serves:
//!
//!   demux (V1) → plan_segments (V2) → { init/media segment (V2), manifests (V3) }
//!
//! Every method here reads the source and calls into `isobmff` / `segment` /
//! `manifest`, whose interesting bodies are the `todo!()`s. So a real request walks
//! this wiring and lands on the first unimplemented vertical — that panic is the
//! worklist.
//!
//! TODO(caching, horizontal): each call re-reads and re-demuxes the source. Memoize
//! the demuxed `MediaInfo`, the `SegmentIndex`, and the built init/segment `Bytes`
//! (keyed by asset/rendition[/index]) so a hot segment is cut once, not per request.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use bytes::Bytes;

use crate::error::AppError;
use crate::isobmff::{self, MediaInfo};
use crate::manifest::{self, RenditionInfo};
use crate::segment::{self, SegmentIndex};

/// One rung of an asset's bitrate ladder: a source file on disk.
#[derive(Debug, Clone)]
pub struct Rendition {
    /// Rendition id (the source filename stem, e.g. `720p`).
    pub id: String,
    /// Absolute path to the source `.mp4`.
    pub source_path: PathBuf,
    // TODO(V3/V4): fill `bandwidth`/`width`/`height` from the demuxed track (probe
    // the source once at load, or lazily) so the master playlist advertises a real
    // ladder. Left at 0 so the scaffold has no fabricated numbers.
    pub bandwidth: u32,
    pub width: u16,
    pub height: u16,
}

/// One asset (a single title) and its renditions, keyed by rendition id.
#[derive(Debug, Clone)]
pub struct Asset {
    pub name: String,
    pub renditions: BTreeMap<String, Rendition>,
}

/// The whole media library plus the segmenting target.
pub struct Catalog {
    assets: BTreeMap<String, Asset>,
    target_segment_secs: f64,
}

impl Catalog {
    /// Scan `MEDIA_DIR/<asset>/<rendition>.mp4` into an in-memory catalog. Missing
    /// dir ⇒ an empty (but valid) library, so the server still starts.
    pub async fn load(
        media_dir: impl AsRef<Path>,
        target_segment_secs: f64,
    ) -> std::io::Result<Self> {
        let media_dir = media_dir.as_ref();
        let mut assets = BTreeMap::new();

        if !media_dir.exists() {
            tracing::warn!(dir = %media_dir.display(), "MEDIA_DIR does not exist — starting with an empty library");
            std::fs::create_dir_all(media_dir)?;
        }

        for asset_entry in std::fs::read_dir(media_dir)? {
            let asset_entry = asset_entry?;
            if !asset_entry.file_type()?.is_dir() {
                continue;
            }
            let asset_name = asset_entry.file_name().to_string_lossy().into_owned();
            let mut renditions = BTreeMap::new();

            for rend in std::fs::read_dir(asset_entry.path())? {
                let rend = rend?;
                let path = rend.path();
                if path.extension().and_then(|e| e.to_str()) != Some("mp4") {
                    continue;
                }
                let Some(id) = path.file_stem().and_then(|s| s.to_str()).map(str::to_owned) else {
                    continue;
                };
                renditions.insert(
                    id.clone(),
                    Rendition {
                        id,
                        source_path: path,
                        bandwidth: 0,
                        width: 0,
                        height: 0,
                    },
                );
            }

            if renditions.is_empty() {
                continue;
            }
            tracing::info!(asset = %asset_name, renditions = renditions.len(), "loaded asset");
            assets.insert(
                asset_name.clone(),
                Asset {
                    name: asset_name,
                    renditions,
                },
            );
        }

        tracing::info!(assets = assets.len(), "media library scanned");
        Ok(Self {
            assets,
            target_segment_secs,
        })
    }

    /// Asset names, sorted — for `GET /assets`.
    pub fn asset_names(&self) -> Vec<&str> {
        self.assets.keys().map(String::as_str).collect()
    }

    /// Rendition ids for an asset (None if the asset is unknown) — for `GET /assets`.
    pub fn rendition_ids(&self, asset: &str) -> Option<Vec<&str>> {
        self.assets
            .get(asset)
            .map(|a| a.renditions.keys().map(String::as_str).collect())
    }

    // -- lookups (plumbing: map missing → the right 404) ---------------------

    fn asset(&self, asset: &str) -> Result<&Asset, AppError> {
        self.assets.get(asset).ok_or(AppError::UnknownAsset)
    }

    fn rendition(&self, asset: &str, rendition: &str) -> Result<&Rendition, AppError> {
        self.asset(asset)?
            .renditions
            .get(rendition)
            .ok_or(AppError::UnknownRendition)
    }

    /// Read a rendition's source file and demux it into a sample table (V1).
    async fn demux(&self, rendition: &Rendition) -> Result<(Vec<u8>, MediaInfo), AppError> {
        let source = tokio::fs::read(&rendition.source_path).await?;
        let media = isobmff::demux(&source)?;
        Ok((source, media))
    }

    // -- packaging pipeline (composes the verticals) -------------------------

    /// HLS master playlist for an asset — the ABR ladder (V3/V4).
    pub fn master_playlist(&self, asset: &str) -> Result<String, AppError> {
        let asset = self.asset(asset)?;
        let renditions: Vec<RenditionInfo> = asset
            .renditions
            .values()
            .map(|r| RenditionInfo {
                id: r.id.clone(),
                bandwidth: r.bandwidth,
                width: r.width,
                height: r.height,
                uri: format!("{}/index.m3u8", r.id),
            })
            .collect();
        Ok(manifest::hls_master_playlist(&renditions))
    }

    /// HLS media playlist for one rendition (V1 → V2 → V3).
    pub async fn media_playlist(&self, asset: &str, rendition: &str) -> Result<String, AppError> {
        let rendition = self.rendition(asset, rendition)?;
        let (_source, media) = self.demux(rendition).await?;
        let track = media.primary_video()?;
        let index = segment::plan_segments(track, self.target_segment_secs)?;
        Ok(manifest::hls_media_playlist(
            &index,
            track,
            self.target_segment_secs,
        ))
    }

    /// DASH MPD for one rendition (V1 → V2 → V3).
    pub async fn dash_mpd(&self, asset: &str, rendition: &str) -> Result<String, AppError> {
        let rendition = self.rendition(asset, rendition)?;
        let (_source, media) = self.demux(rendition).await?;
        let track = media.primary_video()?;
        let index = segment::plan_segments(track, self.target_segment_secs)?;
        Ok(manifest::dash_mpd(&index, track))
    }

    /// CMAF init segment for one rendition (V1 → V2).
    pub async fn init_segment(&self, asset: &str, rendition: &str) -> Result<Bytes, AppError> {
        let rendition = self.rendition(asset, rendition)?;
        let (_source, media) = self.demux(rendition).await?;
        let track = media.primary_video()?;
        segment::build_init_segment(track)
    }

    /// One media segment for one rendition (V1 → V2). Bounds `index` against the plan.
    pub async fn media_segment(
        &self,
        asset: &str,
        rendition: &str,
        index: usize,
    ) -> Result<Bytes, AppError> {
        let rendition = self.rendition(asset, rendition)?;
        let (source, media) = self.demux(rendition).await?;
        let track = media.primary_video()?;
        let plan: SegmentIndex = segment::plan_segments(track, self.target_segment_secs)?;
        let entry = plan
            .segments
            .get(index)
            .ok_or(AppError::SegmentOutOfRange)?;
        segment::build_media_segment(&source, track, entry)
    }
}

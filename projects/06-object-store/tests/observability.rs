//! Black-box assertions for the Prometheus observability surface.

use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};
use tempfile::TempDir;
use tower::ServiceExt;

async fn request(
    app: &Router,
    method: Method,
    uri: &str,
    body: Body,
) -> (StatusCode, bytes::Bytes) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(uri)
                .body(body)
                .unwrap(),
        )
        .await
        .expect("router is infallible");
    let status = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("collect response body")
        .to_bytes();
    (status, body)
}

#[tokio::test]
async fn metrics_report_object_lifecycle_dedup_ranges_and_occupancy() {
    let handle = object_store::metrics::install();
    let dir = TempDir::new().expect("temp data dir");
    let state = AppState::open(dir.path(), DEFAULT_MAX_OBJECT_SIZE).expect("open app state");
    let app = routes::router(state).merge(routes::metrics_router(handle));

    assert_eq!(
        request(&app, Method::PUT, "/photos", Body::empty()).await.0,
        StatusCode::OK
    );
    assert_eq!(
        request(&app, Method::PUT, "/photos/first.txt", Body::from("abc"))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        request(&app, Method::PUT, "/photos/copy.txt", Body::from("abc"))
            .await
            .0,
        StatusCode::OK
    );

    let (status, body) = request(&app, Method::GET, "/photos/first.txt", Body::empty()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, "abc");

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::GET)
                .uri("/photos/first.txt")
                .header(header::RANGE, "bytes=1-2")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .expect("router is infallible");
    assert_eq!(response.status(), StatusCode::PARTIAL_CONTENT);
    assert_eq!(
        response
            .into_body()
            .collect()
            .await
            .expect("collect range body")
            .to_bytes(),
        "bc"
    );

    assert_eq!(
        request(&app, Method::DELETE, "/photos/first.txt", Body::empty())
            .await
            .0,
        StatusCode::NO_CONTENT
    );

    let (_, metrics) = request(&app, Method::GET, "/metrics", Body::empty()).await;
    let metrics = std::str::from_utf8(&metrics).expect("metrics are UTF-8");

    assert!(metrics.contains("object_store_objects_put_total 2"));
    assert!(metrics.contains("object_store_objects_get_total 2"));
    assert!(metrics.contains("object_store_objects_deleted_total 1"));
    assert!(metrics.contains("object_store_dedup_hits_total 1"));
    assert!(metrics.contains("object_store_range_requests_served_total 1"));
    assert!(metrics.contains("object_store_blob_count 1"));
    assert!(metrics.contains("object_store_total_bytes_stored 3"));
    assert!(metrics.contains("object_store_object_size_bytes_count 2"));
    assert!(metrics.contains("object_store_upload_throughput_bytes_per_second_count 2"));
    assert!(metrics.contains("object_store_download_throughput_bytes_per_second_count 2"));
}

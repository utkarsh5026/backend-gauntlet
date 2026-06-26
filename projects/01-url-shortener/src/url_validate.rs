//! URL validation + normalization for submitted long URLs (security checklist).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use url::Url;

use crate::error::AppError;

/// Maximum length of a normalized URL we are willing to store, in bytes.
const MAX_URL_LEN: usize = 2048;

/// Parse, validate, and normalize a submitted long URL for storage.
///
/// Enforces the security checklist for user-supplied URLs: only `https` is
/// accepted, and hosts that point at internal infrastructure (loopback,
/// private/link-local ranges, CGNAT, `localhost`, and friends) are rejected to
/// prevent SSRF via the redirect. On success the URL is normalized — the
/// fragment is dropped and a redundant `:443` port is removed — so equivalent
/// inputs dedupe to the same stored string.
///
/// # Errors
///
/// Returns [`AppError::BadRequest`] when the input is empty, unparseable, not
/// `https`, has no host, resolves to a [blocked host](is_blocked_host), or whose
/// normalized form exceeds [`MAX_URL_LEN`].
///
/// # Examples
///
/// ```ignore
/// let url = validate_long_url("  https://example.com:443/x#frag  ").unwrap();
/// assert_eq!(url, "https://example.com/x");
///
/// assert!(validate_long_url("http://example.com").is_err());
/// assert!(validate_long_url("https://127.0.0.1/").is_err());
/// ```
pub fn validate_long_url(input: &str) -> Result<String, AppError> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err(AppError::BadRequest("invalid URL".into()));
    }

    let mut url = Url::parse(trimmed).map_err(|_| AppError::BadRequest("invalid URL".into()))?;

    if !url.scheme().eq_ignore_ascii_case("https") {
        return Err(AppError::BadRequest("only https URLs are allowed".into()));
    }

    let host = url
        .host_str()
        .ok_or_else(|| AppError::BadRequest("invalid URL".into()))?;

    if is_blocked_host(host) {
        return Err(AppError::BadRequest("internal URLs are not allowed".into()));
    }

    // Fragment is not sent on redirect; drop it so equivalent URLs dedupe cleanly.
    url.set_fragment(None);
    if url.port() == Some(443) {
        let _ = url.set_port(None);
    }

    let normalized = url.to_string();
    if normalized.len() > MAX_URL_LEN {
        return Err(AppError::BadRequest("URL too long".into()));
    }

    Ok(normalized)
}

/// Report whether a URL host should be refused as an SSRF risk.
///
/// A host that parses as a literal IP is checked against [`is_blocked_ip`];
/// otherwise it is matched by name via [`is_blocked_hostname`]. Note this only
/// inspects the host as written — it does not resolve DNS, so a public name that
/// resolves to a private address is not caught here.
fn is_blocked_host(host: &str) -> bool {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return is_blocked_ip(ip);
    }
    is_blocked_hostname(host)
}

/// Block hostnames that conventionally resolve to local/internal endpoints.
///
/// Strips a trailing root dot and matches case-insensitively against `localhost`
/// and the `.localhost`, `.local`, and `.internal` suffixes.
fn is_blocked_hostname(host: &str) -> bool {
    let host = host.trim_end_matches('.');
    let lower = host.to_ascii_lowercase();
    lower == "localhost"
        || lower.ends_with(".localhost")
        || lower.ends_with(".local")
        || lower.ends_with(".internal")
}

/// Block IP literals that are not safely routable to the public internet.
///
/// Covers loopback, private, link-local (incl. the `169.254.169.254` cloud
/// metadata endpoint), unspecified, broadcast, the `0.0.0.0/8` range, and CGNAT
/// for IPv4; IPv6 is delegated to [`is_blocked_ipv6`].
fn is_blocked_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            ip.is_loopback()
                || ip.is_private()
                || ip.is_link_local()
                || ip.is_unspecified()
                || ip.is_broadcast()
                || ip.octets()[0] == 0
                || is_cgnat_ipv4(ip)
        }
        IpAddr::V6(v6) => is_blocked_ipv6(v6),
    }
}

/// 100.64.0.0/10 — shared address space (RFC 6598), often reachable internally.
fn is_cgnat_ipv4(ip: Ipv4Addr) -> bool {
    let [a, b, _, _] = ip.octets();
    a == 100 && (b & 0xC0) == 64
}

/// Block IPv6 literals that target the local host or an internal network.
fn is_blocked_ipv6(ip: Ipv6Addr) -> bool {
    ip.is_loopback() || ip.is_unspecified() || is_unique_local_ipv6(ip) || is_link_local_ipv6(ip)
}

/// Match the IPv6 unique-local range `fc00::/7` (RFC 4193), the IPv6 analogue of
/// private address space.
fn is_unique_local_ipv6(ip: Ipv6Addr) -> bool {
    (ip.segments()[0] & 0xFE00) == 0xFC00
}

/// Match the IPv6 link-local range `fe80::/10` (RFC 4291).
fn is_link_local_ipv6(ip: Ipv6Addr) -> bool {
    (ip.segments()[0] & 0xFFC0) == 0xFE80
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_public_https_url() {
        let url = validate_long_url("https://example.com/path?q=1").unwrap();
        assert_eq!(url, "https://example.com/path?q=1");
    }

    #[test]
    fn trims_whitespace() {
        let url = validate_long_url("  https://example.com  ").unwrap();
        assert_eq!(url, "https://example.com/");
    }

    #[test]
    fn rejects_http_scheme() {
        assert!(validate_long_url("http://example.com").is_err());
    }

    #[test]
    fn rejects_javascript_scheme() {
        assert!(validate_long_url("javascript:alert(1)").is_err());
    }

    #[test]
    fn rejects_loopback_ipv4() {
        assert!(validate_long_url("https://127.0.0.1/").is_err());
    }

    #[test]
    fn rejects_private_ipv4() {
        assert!(validate_long_url("https://192.168.1.1/").is_err());
        assert!(validate_long_url("https://10.0.0.1/").is_err());
    }

    #[test]
    fn rejects_link_local_metadata_ip() {
        assert!(validate_long_url("https://169.254.169.254/").is_err());
    }

    #[test]
    fn rejects_localhost_hostname() {
        assert!(validate_long_url("https://localhost/").is_err());
    }

    #[test]
    fn strips_fragment_and_default_https_port() {
        let url = validate_long_url("https://example.com:443/x#section").unwrap();
        assert_eq!(url, "https://example.com/x");
    }

    #[test]
    fn allows_http_in_query_string() {
        let url = validate_long_url("https://example.com?next=http://other.example").unwrap();
        assert_eq!(url, "https://example.com/?next=http://other.example");
    }
}

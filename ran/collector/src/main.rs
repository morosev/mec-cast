//! ran-collector binary.
//!
//! Environment:
//!   GNB_METRICS_SOURCE  udp | ws | auto (default auto: listen on both, lock
//!                       onto whichever srsRAN delivers on first)
//!   GNB_METRICS_ADDR    UDP bind address (default 0.0.0.0:55555 — point the
//!                       srsRAN gnb.yml `metrics.addr/port` here, ≤24.x)
//!   GNB_METRICS_WS      srsRAN `remote_control` WebSocket, host:port or
//!                       ws:// URL (default 127.0.0.1:8001, 25.04+)
//!   PTP_DEVICE          PHC for ptp.reliable, e.g. /dev/ptp0 (optional)
//!   RUN_ID              experiment run id (default "dev-run")
//!   LOGGING_URL       mec-cast-logging-service base URL (optional)
//!   RUNS_DIR          base output directory (default "runs")
//!   ADMIN_URL         admin service, e.g. ws://edge:8099/ws/node (optional)
//!
//! With no ADMIN_URL the collector records immediately under the environment's
//! RUN_ID, exactly as it always has. With one, the run lifecycle moves to the
//! admin and RUN_ID is ignored — see ADR-0007.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use ran_collector::source::{self, SourceKind};
use ran_collector::{run, CollectorConfig};

/// An unset variable and an empty one mean the same here: compose passes
/// `${VAR:-}`, so "not configured" routinely arrives as "".
fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|s| !s.is_empty())
}

fn main() -> std::io::Result<()> {
    let kind = SourceKind::parse(&env("GNB_METRICS_SOURCE").unwrap_or_default())
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
    let udp = env("GNB_METRICS_ADDR").unwrap_or_else(|| "0.0.0.0:55555".into());
    let ws = env("GNB_METRICS_WS").unwrap_or_else(|| source::DEFAULT_WS.into());
    // Empty RUN_ID is how the lab compose says "the admin names the runs";
    // standalone, it falls back to dev-run exactly as an unset one always did.
    let run_id = env("RUN_ID").unwrap_or_else(|| "dev-run".into());
    let runs_dir = env("RUNS_DIR").unwrap_or_else(|| "runs".into());

    let mut cfg = CollectorConfig::new(run_id.clone(), runs_dir);
    cfg.logging_url = env("LOGGING_URL");
    cfg.ptp_device = env("PTP_DEVICE");

    let metrics = source::open(kind, &udp, &ws)?;
    eprintln!(
        "[ran-collector] source={} udp={udp} ws={ws} (run_id={run_id})",
        kind.as_str()
    );

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        ctrlc_handler(move || stop.store(true, Ordering::SeqCst));
    }

    let admin_url = std::env::var("ADMIN_URL").unwrap_or_default();
    let report = if admin_url.is_empty() {
        run(metrics, cfg, &stop)?
    } else {
        #[cfg(feature = "admin")]
        {
            let host = hostname();
            eprintln!("[ran-collector] admin at {admin_url} (host={host})");
            // The admin names the runs; each session derives its own
            // <runs_dir>/<run_id>/ran/ from the base cfg carries.
            ran_collector::run_with_admin(
                metrics,
                cfg,
                Arc::clone(&stop),
                ran_collector::admin::AdminConfig::new(admin_url, host, 0),
            )?
        }
        #[cfg(not(feature = "admin"))]
        {
            eprintln!("[ran-collector] ADMIN_URL set but this build has no admin feature");
            run(metrics, cfg, &stop)?
        }
    };
    eprintln!("[ran-collector] done: {report:?}");
    Ok(())
}

/// This machine's name, for the stable node id the admin addresses.
#[cfg(feature = "admin")]
fn hostname() -> String {
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("HOSTNAME").ok())
        .unwrap_or_else(|| "unknown".into())
}

/// Minimal SIGINT/SIGTERM hook without external crates.
fn ctrlc_handler<F: Fn() + Send + Sync + 'static>(f: F) {
    use std::sync::OnceLock;
    static HANDLER: OnceLock<Box<dyn Fn() + Send + Sync>> = OnceLock::new();
    let _ = HANDLER.set(Box::new(f));

    extern "C" fn trampoline(_: libc::c_int) {
        if let Some(h) = HANDLER.get() {
            h();
        }
    }
    // SAFETY: installing a signal handler that only flips an atomic flag.
    unsafe {
        let handler = trampoline as extern "C" fn(libc::c_int) as *const () as libc::sighandler_t;
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGTERM, handler);
    }
}

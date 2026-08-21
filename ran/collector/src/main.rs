//! ran-collector binary.
//!
//! Environment:
//!   GNB_METRICS_ADDR  UDP bind address (default 0.0.0.0:55555 — point the
//!                     srsRAN gnb.yml `metrics.addr/port` here)
//!   RUN_ID            experiment run id (default "dev-run")
//!   LOGGING_URL       mec-cast-logging-service base URL (optional)
//!   RUNS_DIR          base output directory (default "runs")
//!   ADMIN_URL         admin service, e.g. ws://edge:8099/ws/node (optional)
//!
//! With no ADMIN_URL the collector records immediately under the environment's
//! RUN_ID, exactly as it always has. With one, the run lifecycle moves to the
//! admin and RUN_ID is ignored — see ADR-0007.

use std::net::UdpSocket;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use ran_collector::{run, CollectorConfig};

fn main() -> std::io::Result<()> {
    let bind = std::env::var("GNB_METRICS_ADDR").unwrap_or_else(|_| "0.0.0.0:55555".into());
    let run_id = std::env::var("RUN_ID").unwrap_or_else(|_| "dev-run".into());
    let runs_dir = std::env::var("RUNS_DIR").unwrap_or_else(|_| "runs".into());

    let mut cfg = CollectorConfig::new(
        run_id.clone(),
        std::path::Path::new(&runs_dir).join(&run_id).join("ran"),
    );
    cfg.logging_url = std::env::var("LOGGING_URL").ok().filter(|s| !s.is_empty());

    let socket = UdpSocket::bind(&bind)?;
    eprintln!("[ran-collector] listening on {bind} (run_id={run_id})");

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        ctrlc_handler(move || stop.store(true, Ordering::SeqCst));
    }

    let admin_url = std::env::var("ADMIN_URL").unwrap_or_default();
    let report = if admin_url.is_empty() {
        run(socket, cfg, &stop)?
    } else {
        #[cfg(feature = "admin")]
        {
            let host = hostname();
            eprintln!("[ran-collector] admin at {admin_url} (host={host})");
            // The admin names the runs, so out_dir is rebased per run inside
            // the session; cfg carries the base.
            ran_collector::run_with_admin(
                socket,
                cfg,
                Arc::clone(&stop),
                ran_collector::admin::AdminConfig::new(admin_url, host, 0),
            )?
        }
        #[cfg(not(feature = "admin"))]
        {
            eprintln!("[ran-collector] ADMIN_URL set but this build has no admin feature");
            run(socket, cfg, &stop)?
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

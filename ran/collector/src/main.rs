//! ran-collector binary.
//!
//! Environment:
//!   GNB_METRICS_ADDR  UDP bind address (default 0.0.0.0:55555 — point the
//!                     srsRAN gnb.yml `metrics.addr/port` here)
//!   RUN_ID            experiment run id (default "dev-run")
//!   LOGGING_URL       mec-cast-logging-service base URL (optional)
//!   RUNS_DIR          base output directory (default "runs")

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

    let report = run(socket, cfg, &stop)?;
    eprintln!("[ran-collector] done: {report:?}");
    Ok(())
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

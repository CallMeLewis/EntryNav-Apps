//! mhd-maps: MHD+ live controls for the MHD+ Suite app (src/mhd_maps.cpp). A port of src/collector.c's MHD_MAPS_ONLY
//! worker that sends the same requests in the same order, prints the same line and exits the same way.
//!   mhd-maps --host IPv4 --map 1..4      asks the DME for that map
//!   mhd-maps --host IPv4 --cooling 0|1   Max Cooling off or on; on only with the engine running
//! Map switching and Max Cooling aren't BMW jobs: MHD's code in the DME answers them. The commands are the ones
//! cauliflowermossgrape's Live Adjustments sends (apps/live_adjustments/tune.js), from a1-mapping's captures, and work
//! on an MG1 with an MHD+ multi-map tune:
//!     map         2F 01 <map - 1>   ->  6F 01 <map - 1>
//!     Max Cooling 2F 03 01 | 00     ->  6F 03 01 | 00
//! A request counts only when the DME echoes it. A DME without MHD refuses it. Max Cooling on first reads OBD's engine
//! speed (01 0C) and sends nothing when the engine isn't running. No read-back of the active map is known, so nothing
//! is read first and the line says accepted, not confirmed. --search-dev, --search-port and --gateway-file are for the
//! tests. Exit 0 when the DME accepted the request, 1 otherwise, 2 for wrong arguments.

use ena_diag::gateway::{GatewaySearch, SEARCH_FAILURES};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway};
use ena_diag::{arguments, mono_ms, number, pause_ms, program_name, running, warn, Output, EADDRINUSE};
use std::os::fd::OwnedFd;

const DME: u8 = 0x12;
const MAP: u8 = 0x01;
const COOLING: u8 = 0x03;
const TIMEOUT_MS: u64 = 3000;
/// Live Adjustments' threshold for "running": over 500 rpm, from OBD's engine speed (01 0C, quarter rpm).
const COOLING_RPM: i32 = 500;
/// What the C reads the echo into, and the engine speed: a longer answer fails the request.
const ECHO_REPLY: usize = 16;
const RPM_REPLY: usize = 8;

/// Sends 2F function value and waits for its echo (mhd_send).
fn send(fd: &OwnedFd, function: u8, value: u8) -> Reply {
    exchange(
        fd,
        DME,
        &[0x2F, function, value],
        &[0x6F, function, value],
        ECHO_REPLY,
        TIMEOUT_MS,
    )
}

/// Engine speed in rpm, or -1 when the DME doesn't give it (mhd_engine_rpm).
fn engine_rpm(fd: &OwnedFd) -> i32 {
    match exchange(fd, DME, &[0x01, 0x0C], &[0x41, 0x0C], RPM_REPLY, 1000) {
        Reply::Data(r) if r.len() == 4 => i32::from(u16::from_be_bytes([r[2], r[3]]) / 4),
        _ => -1,
    }
}

/// The line for a request's answer, and the exit status (mhd_result).
fn result(out: &mut Output, reply: &Reply, what: &str, accepted: &str) -> i32 {
    match reply {
        Reply::Data(d) if !d.is_empty() => {
            out.line(accepted);
            return 0;
        }
        Reply::Refused(nrc @ (0x11 | 0x12 | 0x13 | 0x31)) => {
            out.line(format!(
                "The DME refused {what} (NRC {nrc:02X}). Is an MHD+ tune flashed?"
            ));
        }
        Reply::Refused(nrc) => out.line(format!("The DME refused {what} (NRC {nrc:02X})")),
        _ => out.line(format!("No answer from the DME for {what}")),
    }
    1
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Action {
    None,
    Map,
    Cooling,
}

fn main() {
    let args = arguments();
    let mut host_option: Option<&[u8]> = None;
    let mut port = 6801u16;
    let mut action = Action::None;
    let mut value = 0;
    let mut search = GatewaySearch::default();
    let mut i = 1;
    while i < args.len() {
        let has_next = i + 1 < args.len();
        let has_value = has_next && action == Action::None;
        match args[i].as_slice() {
            b"--host" if has_next && host_option.is_none() => {
                i += 1;
                host_option = Some(&args[i]);
            }
            b"--port" if has_next => {
                i += 1;
                port = number(&args[i], 1, 65535) as u16;
            }
            b"--map" if has_value => {
                i += 1;
                action = Action::Map;
                value = number(&args[i], 1, 4);
            }
            b"--cooling" if has_value => {
                i += 1;
                action = Action::Cooling;
                value = number(&args[i], 0, 1);
            }
            _ if search.option(&args, &mut i) => {}
            _ => std::process::exit(2),
        }
        i += 1;
    }
    // The C copies the host into 16 bytes (INET_ADDRSTRLEN) only when it fits.
    let mut host = host_option
        .filter(|h| h.len() < 16)
        .map(<[u8]>::to_vec)
        .unwrap_or_default();
    if host.is_empty() || action == Action::None {
        warn(
            &[
                b"Usage: ",
                program_name(&args),
                b" --host IPv4 [--port 6801] (--map 1-4 | --cooling 0-1)\n",
            ]
            .concat(),
        );
        std::process::exit(2);
    }
    ena_diag::handle_signals();
    let mut out = Output::default();
    let status = run(&mut out, &mut host, port, &mut search, action, value);
    out.flush();
    std::process::exit(status);
}

fn run(out: &mut Output, host: &mut Vec<u8>, port: u16, search: &mut GatewaySearch, action: Action, value: u32) -> i32 {
    // A gauge app still holding the gateway gives it up within a lease once asked (reserve_gateway asks).
    let mut lock = reserve_gateway();
    let deadline = mono_ms() + 650;
    while running() && lock.as_ref().err() == Some(&EADDRINUSE) && mono_ms() < deadline {
        pause_ms(25);
        lock = reserve_gateway();
    }
    let Ok(lock) = lock else {
        out.line("Diagnostics busy. Wait a moment and try again.");
        return 1;
    };
    let mut gateway = connect_gw(host, port);
    if gateway.is_none() {
        search.failures = SEARCH_FAILURES - 1; // a one-shot run searches at once
        if search.after_failure(host, port) {
            gateway = connect_gw(host, port);
        }
    }
    let Some(fd) = gateway else {
        out.line("Diagnostic gateway unavailable");
        release_gateway(None, Some(lock));
        return 1;
    };
    let status = if action == Action::Map {
        let reply = send(&fd, MAP, (value - 1) as u8);
        result(
            out,
            &reply,
            &format!("map {value}"),
            &format!("Map {value} accepted by the DME"),
        )
    } else if value != 0 && engine_rpm(&fd) <= COOLING_RPM {
        out.line("Start the engine first. Max Cooling needs it running.");
        1
    } else {
        let reply = send(&fd, COOLING, value as u8);
        if value != 0 {
            result(out, &reply, "Max Cooling on", "Max Cooling on, accepted by the DME")
        } else {
            result(out, &reply, "Max Cooling off", "Max Cooling off, accepted by the DME")
        }
    };
    release_gateway(Some(fd), Some(lock));
    status
}

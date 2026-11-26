//! ena-gateway: finds the diagnostic gateway from the head unit and checks the route to it, for setup.sh. A port of
//! src/collector.c's GATEWAY_TOOL that prints the same lines and exits the same way.
//!   ena-gateway discover [--dev NAME | --to IPv4] [--port 6811] [--wait MS]
//!     HSFZ vehicle identification on every IPv4 network of the head unit's car-side Ethernet (vbr0, or --dev). Prints
//!     each responder from those networks that calls itself the gateway (DIAGADR10), one per line. Exit 0 if one
//!     answered, 1 if none, 2 when nothing could be sent. --to sends to one address instead and accepts any responder,
//!     for tests.
//!   ena-gateway check --host IPv4 [--port 6801]
//!     Reads OBD 01 00 (the DME's supported PIDs) through the gateway. It only reads. Exit 0 on an answer, positive or
//!     negative.

use ena_diag::gateway::{find_gateways, SearchError, MAX_GATEWAYS};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway};
use ena_diag::{arguments, number, program_name, warn, Output};

fn discover(out: &mut Output, device: &[u8], to: Option<&[u8]>, port: u16, wait_ms: u64) -> i32 {
    match find_gateways(device, to, port, wait_ms, MAX_GATEWAYS) {
        Err(SearchError::NoAddress) => 1,
        Err(SearchError::NotSent) => 2,
        Ok(found) => {
            for gateway in &found {
                out.line(gateway.to_string());
            }
            if found.is_empty() {
                1
            } else {
                0
            }
        }
    }
}

/// before, then host as the command line gave it, then after.
fn about(before: &str, host: &[u8], after: &str) -> Vec<u8> {
    [before.as_bytes(), host, after.as_bytes()].concat()
}

fn check(out: &mut Output, host: &[u8], port: u16) -> i32 {
    let Ok(lock) = reserve_gateway() else {
        out.line("Diagnostics busy. Close BoostDeck or ShiftDeck and try again.");
        return 1;
    };
    let Some(fd) = connect_gw(host, port) else {
        out.line(about("No connection to ", host, &format!(" port {port}")));
        return 1;
    };
    let reply = exchange(&fd, 0x12, &[0x01, 0x00], &[0x41, 0x00], 16, 1500);
    release_gateway(Some(fd), Some(lock));
    match reply {
        Reply::Data(_) => {
            out.line(about("Gateway ", host, " answers; the DME replied to OBD 01 00"));
            0
        }
        Reply::Refused(nrc) => {
            out.line(about(
                "Gateway ",
                host,
                &format!(" answers; the DME refused OBD 01 00 (NRC {nrc:02X})"),
            ));
            0
        }
        Reply::Failed => {
            out.line(about("Connected to ", host, ", but the DME did not answer"));
            1
        }
    }
}

fn main() {
    let args = arguments();
    if args.len() < 2 || (args[1] != b"discover" && args[1] != b"check") {
        let mut usage = b"Usage: ".to_vec();
        usage.extend_from_slice(program_name(&args));
        usage.extend_from_slice(
            b" discover [--dev NAME | --to IPv4] [--port 6811] [--wait MS] | \
                                  check --host IPv4 [--port 6801]\n",
        );
        warn(&usage);
        std::process::exit(2);
    }
    let discovering = args[1] == b"discover";
    let (mut host, mut to): (Option<&[u8]>, Option<&[u8]>) = (None, None);
    let mut device: &[u8] = b"vbr0";
    let (mut port, mut wait_ms) = (0u32, 1800u32);
    let mut i = 2;
    while i < args.len() {
        let has_value = i + 1 < args.len();
        match args[i].as_slice() {
            b"--host" if !discovering && has_value && host.is_none() => {
                i += 1;
                host = Some(&args[i]);
            }
            b"--to" if discovering && has_value && to.is_none() => {
                i += 1;
                to = Some(&args[i]);
            }
            b"--dev" if discovering && has_value => {
                i += 1;
                device = &args[i];
            }
            b"--wait" if discovering && has_value => {
                i += 1;
                wait_ms = number(&args[i], 100, 10000);
            }
            b"--port" if has_value => {
                i += 1;
                port = number(&args[i], 1, 65535);
            }
            other => {
                let mut message = b"Unknown argument: ".to_vec();
                message.extend_from_slice(other);
                message.push(b'\n');
                warn(&message);
                std::process::exit(2);
            }
        }
        i += 1;
    }
    ena_diag::handle_signals();
    let mut out = Output::default();
    let code = if discovering {
        discover(
            &mut out,
            device,
            to,
            if port != 0 { port as u16 } else { 6811 },
            u64::from(wait_ms),
        )
    } else if let Some(host) = host {
        check(&mut out, host, if port != 0 { port as u16 } else { 6801 })
    } else {
        warn(b"check needs --host\n");
        2
    };
    out.flush();
    std::process::exit(code);
}

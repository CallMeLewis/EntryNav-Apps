//! engine-codes: the DME's fault memory, for the Engine Fault Codes app (src/engine_codes.cpp). A port of
//! src/collector.c's ENGINE_CODES_ONLY worker that prints the same lines and exits the same way. It prints a header
//! line, then one line per code, "XXXXXX  status SS", which the app pages through and describes.
//!   engine-codes --host IPv4 --codes   reads the codes; only reads
//!   engine-codes --host IPv4 --clear   clears the fault memory (UDS 14 FF FF FF), then reads the codes again
//! --search-dev, --search-port and --gateway-file are for the tests, as in the other workers. Exit 0 once it has
//! printed its result, 2 for wrong arguments.

use ena_diag::gateway::{GatewaySearch, SEARCH_FAILURES};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway};
use ena_diag::{arguments, mono_ms, number, pause_ms, program_name, running, warn, Output, EADDRINUSE};
use std::os::fd::OwnedFd;

const DME: u8 = 0x12;
/// Fault, pending, confirmed, failed since clear, or warning requested. Bits 0x10 and 0x40 only report incomplete
/// tests.
const FAULT_STATUS: u8 = 0xaf;
const SHOWN: usize = 128;
const REPLY_LIMIT: usize = 4094;

/// The lines for the answer to 19 02 AF.
fn codes_report(out: &mut Output, reply: &Reply, clear: bool) {
    let codes = match reply {
        Reply::Refused(nrc) => return out.line(format!("Read rejected (NRC {nrc:02X})")),
        Reply::Data(data) if data.len() >= 3 && (data.len() - 3) % 4 == 0 => data,
        _ => {
            return out.line(if clear {
                "Clear acknowledged; recheck unavailable"
            } else {
                "No valid code response"
            })
        }
    };
    let supported = codes[2] & FAULT_STATUS;
    let faults: Vec<&[u8]> = codes[3..].chunks(4).filter(|code| code[3] & supported != 0).collect();
    let count = faults.len();
    out.line(format!(
        "Engine: {count} {}{}",
        if count == 1 { "code" } else { "codes" },
        if clear { " after clear" } else { "" }
    ));
    for code in faults.iter().take(SHOWN) {
        out.line(format!(
            "{:02X}{:02X}{:02X}  status {:02X}",
            code[0], code[1], code[2], code[3]
        ));
    }
    if count > SHOWN {
        out.line("More codes returned; first 128 shown");
    }
}

fn fault_codes(out: &mut Output, fd: Option<&OwnedFd>, clear: bool) {
    let Some(fd) = fd else {
        return out.line("Diagnostic gateway unavailable");
    };
    if clear {
        const CLEAR: [u8; 4] = [0x14, 0xff, 0xff, 0xff];
        let mut n = exchange(fd, DME, &CLEAR, &[0x54], REPLY_LIMIT, 8000);
        // A DME that wants the extended session for a clear: open it, clear once more and go back to the default
        // session. Only after a session-related refusal, and only when the DME accepts the session.
        if matches!(n, Reply::Refused(0x22 | 0x31 | 0x7e | 0x7f))
            && matches!(exchange(fd, DME, &[0x10, 0x03], &[0x50, 0x03], REPLY_LIMIT, 2000),
                        Reply::Data(ref d) if d.len() >= 2)
        {
            n = exchange(fd, DME, &CLEAR, &[0x54], REPLY_LIMIT, 8000);
            exchange(fd, DME, &[0x10, 0x01], &[0x50, 0x01], REPLY_LIMIT, 2000);
        }
        match n {
            Reply::Data(ref d) if d.len() == 1 => {}
            Reply::Refused(nrc @ (0x22 | 0x83 | 0x88)) => {
                return out.line(format!(
                    "Clear refused (NRC {nrc:02X}). Try with the ignition on and the engine off."
                ));
            }
            Reply::Refused(nrc) => return out.line(format!("Clear rejected (NRC {nrc:02X})")),
            _ => return out.line("Clear result unknown: read codes again"),
        }
    }
    let reply = exchange(fd, DME, &[0x19, 0x02, FAULT_STATUS], &[0x59, 0x02], REPLY_LIMIT, 1800);
    codes_report(out, &reply, clear);
}

fn main() {
    let args = arguments();
    let mut host_option: Option<&[u8]> = None;
    let mut port = 6801u16;
    let mut mode = 0;
    let mut search = GatewaySearch::default();
    let mut i = 1;
    while i < args.len() {
        let has_value = i + 1 < args.len();
        match args[i].as_slice() {
            b"--host" if has_value && host_option.is_none() => {
                i += 1;
                host_option = Some(&args[i]);
            }
            b"--port" if has_value => {
                i += 1;
                port = number(&args[i], 1, 65535) as u16;
            }
            b"--codes" if mode == 0 => mode = 1,
            b"--clear" if mode == 0 => mode = 2,
            _ if search.option(&args, &mut i) => {}
            _ => break,
        }
        i += 1;
    }
    // The C copies the host into 16 bytes (INET_ADDRSTRLEN) only when it fits.
    let mut host = host_option
        .filter(|h| h.len() < 16)
        .map(<[u8]>::to_vec)
        .unwrap_or_default();
    if i != args.len() || host.is_empty() || mode == 0 {
        let mut usage = b"Usage: ".to_vec();
        usage.extend_from_slice(program_name(&args));
        usage.extend_from_slice(b" --host IPv4 [--port 6801] (--codes | --clear)\n");
        warn(&usage);
        std::process::exit(2);
    }
    ena_diag::handle_signals();
    let mut out = Output::default();
    // A gauge app still holding the gateway gives it up within a lease once asked (reserve_gateway asks).
    let mut lock = reserve_gateway();
    let deadline = mono_ms() + 650;
    while running() && lock.as_ref().err() == Some(&EADDRINUSE) && mono_ms() < deadline {
        pause_ms(25);
        lock = reserve_gateway();
    }
    let lock = match lock {
        Ok(lock) => lock,
        Err(errno) => {
            out.line(if errno == EADDRINUSE {
                "Diagnostics busy. Wait a moment and try again."
            } else {
                "Could not reserve the diagnostics connection"
            });
            out.flush();
            std::process::exit(0);
        }
    };
    let mut gateway = connect_gw(&host, port);
    if gateway.is_none() {
        search.failures = SEARCH_FAILURES - 1; // a one-shot run searches at once
        if search.after_failure(&mut host, port) {
            gateway = connect_gw(&host, port);
        }
    }
    fault_codes(&mut out, gateway.as_ref(), mode == 2);
    release_gateway(gateway, Some(lock));
    out.flush();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(reply: Reply, clear: bool) -> String {
        let mut out = Output::default();
        codes_report(&mut out, &reply, clear);
        String::from_utf8(out.text().to_vec()).unwrap()
    }

    fn data(hex: &str) -> Reply {
        Reply::Data(
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect(),
        )
    }

    #[test]
    fn only_codes_with_a_fault_bit() {
        // test_engine_codes.py: availability mask FF; 0x2F reports, 0x50 (test not completed, not failed) does not.
        assert_eq!(
            report(data("5902ff1a2f0f2f2222225030fe0108"), false),
            "Engine: 2 codes\n1A2F0F  status 2F\n30FE01  status 08\n"
        );
    }

    #[test]
    fn the_availability_mask_limits_the_bits() {
        assert_eq!(
            report(data("5902011a2f0f2f30fe0108"), false),
            "Engine: 1 code\n1A2F0F  status 2F\n"
        );
    }

    #[test]
    fn none_and_after_a_clear() {
        assert_eq!(report(data("5902ff"), false), "Engine: 0 codes\n");
        assert_eq!(report(data("5902ff"), true), "Engine: 0 codes after clear\n");
    }

    #[test]
    fn a_broken_answer_or_a_refusal() {
        assert_eq!(report(data("5902ff1a2f0f"), false), "No valid code response\n");
        assert_eq!(report(data("5902"), true), "Clear acknowledged; recheck unavailable\n");
        assert_eq!(report(Reply::Failed, false), "No valid code response\n");
        assert_eq!(report(Reply::Refused(0x31), false), "Read rejected (NRC 31)\n");
    }

    #[test]
    fn at_most_128_codes() {
        let mut hex = String::from("5902ff");
        for i in 0..130 {
            hex.push_str(&format!("{:06X}08", i));
        }
        let text = report(data(&hex), false);
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 130);
        assert_eq!(lines[0], "Engine: 130 codes");
        assert_eq!(lines[128], "00007F  status 08");
        assert_eq!(lines[129], "More codes returned; first 128 shown");
    }
}

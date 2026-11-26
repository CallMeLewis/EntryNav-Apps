//! What the Rust workers share, ported from src/collector.c, src/diagnostic_lease.h and src/gauge_core.c: HSFZ to the
//! car's diagnostic gateway and UDS through it (hsfz), the loopback lock every worker takes before it talks to the
//! gateway (lease), finding the gateway on the car-side Ethernet (gateway), and the gauge workers' readings and the
//! frames they send the apps (gauge). Each function keeps the C's timeouts, messages and results, so the workers built
//! on it print and send the same bytes and exit with the same codes.

pub mod gateway;
pub mod gauge;
pub mod hsfz;
pub mod lease;
mod sys;

use std::io::Write;
use std::net::Ipv4Addr;
use std::os::raw::{c_int, c_ulong};
use std::sync::atomic::{AtomicBool, Ordering};

pub use sys::{errno, parent_pid, EADDRINUSE, EINTR};

static RUNNING: AtomicBool = AtomicBool::new(true);

extern "C" fn stop(_: c_int) {
    RUNNING.store(false, Ordering::Relaxed);
}

/// False once SIGINT or SIGTERM arrived: every wait ends early and the worker finishes with what it has.
pub fn running() -> bool {
    RUNNING.load(Ordering::Relaxed)
}

/// Ends the run as a signal would, for a worker that can't go on (collector.c's running = 0).
pub fn end_run() {
    RUNNING.store(false, Ordering::Relaxed);
}

/// SIGINT and SIGTERM end the run cleanly, and SIGPIPE is ignored, as in each C worker's main.
pub fn handle_signals() {
    sys::handle_stop_signals(stop);
}

/// Milliseconds on the monotonic clock (bd_mono_ms): the system's CLOCK_MONOTONIC, shared by every process on the head
/// unit, which the gauge apps compare their frames' stamps with.
pub fn mono_ms() -> u64 {
    sys::monotonic_ms()
}

/// poll(0, 0, ms): 0 after the wait, or the errno of a wait a signal cut short.
pub fn pause_ms(ms: i32) -> i32 {
    if sys::pause(ms) < 0 {
        sys::errno()
    } else {
        0
    }
}

/// poll(0, 0, ms) again after each signal that leaves the run going (pause_ms of the BoostDeck collector).
pub fn sleep_ms(ms: i32) {
    while running() && pause_ms(ms) == EINTR {}
}

/// What a worker prints on stdout, written in one go when it ends, as the C's buffered stdout is on a pipe. Errors
/// writing it are ignored, as puts and printf ignore them.
#[derive(Default)]
pub struct Output(Vec<u8>);

impl Output {
    pub fn line(&mut self, text: impl AsRef<[u8]>) {
        self.0.extend_from_slice(text.as_ref());
        self.0.push(b'\n');
    }

    pub fn text(&self) -> &[u8] {
        &self.0
    }

    pub fn flush(&mut self) {
        let mut stdout = std::io::stdout().lock();
        let _ = stdout.write_all(&self.0);
        let _ = stdout.flush();
        self.0.clear();
    }
}

/// A line on stderr, unbuffered as C's stderr is. Write errors are ignored.
pub fn warn(text: &[u8]) {
    let mut stderr = std::io::stderr().lock();
    let _ = stderr.write_all(text);
}

/// The text of each argument as bytes, as main's argv has them; argv[0] prints as "(null)" when missing, as glibc's
/// printf prints a null string.
pub fn arguments() -> Vec<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    std::env::args_os().map(OsStringExt::into_vec).collect()
}

pub fn program_name(args: &[Vec<u8>]) -> &[u8] {
    args.first().map_or(b"(null)", Vec::as_slice)
}

/// strtoul(s, &end, 10) with the C library's unsigned long: 32 bits on the head unit. Leading white space, a sign
/// (minus wraps), and ULONG_MAX past the top. Returns the value and whether anything is left after the digits, or
/// after s itself when it has none.
pub fn strtoul(s: &[u8]) -> (c_ulong, bool) {
    let mut i = 0;
    while i < s.len() && matches!(s[i], b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r') {
        i += 1;
    }
    let negative = i < s.len() && s[i] == b'-';
    if i < s.len() && (s[i] == b'-' || s[i] == b'+') {
        i += 1;
    }
    let digits = i;
    let mut value: u128 = 0;
    let mut overflow = false;
    while i < s.len() && s[i].is_ascii_digit() {
        value = value * 10 + u128::from(s[i] - b'0');
        if value > c_ulong::MAX as u128 {
            overflow = true;
            value = c_ulong::MAX as u128 + 1; // stays above the limit, never grows
        }
        i += 1;
    }
    if i == digits {
        return (0, !s.is_empty()); // no digits: end is s
    }
    let rest = i < s.len();
    if overflow {
        return (c_ulong::MAX, rest);
    }
    let value = value as c_ulong;
    (if negative { value.wrapping_neg() } else { value }, rest)
}

/// number() of collector.c: a decimal argument from lo to hi, or "Invalid number" and exit 2.
#[allow(clippy::unnecessary_cast)] // unsigned long is 32 bits on the head unit, 64 on a PC
pub fn number(s: &[u8], lo: u32, hi: u32) -> u32 {
    let (n, rest) = strtoul(s);
    if s.is_empty() || rest || n < c_ulong::from(lo) || n > c_ulong::from(hi) {
        let mut message = b"Invalid number: ".to_vec();
        message.extend_from_slice(s);
        message.push(b'\n');
        warn(&message);
        std::process::exit(2);
    }
    n as u32
}

/// inet_pton(AF_INET): four decimal octets with no leading zeros, as glibc reads them.
pub fn ipv4(s: &[u8]) -> Option<Ipv4Addr> {
    std::str::from_utf8(s).ok()?.parse().ok()
}

/// strerror() with the car's glibc wording for the errors a socket call can give here, so messages read the same as
/// the C worker's.
pub fn strerror(errno: i32) -> String {
    let text = match errno {
        1 => "Operation not permitted",
        4 => "Interrupted system call",
        11 => "Resource temporarily unavailable",
        12 => "Cannot allocate memory",
        13 => "Permission denied",
        19 => "No such device",
        22 => "Invalid argument",
        23 => "Too many open files in system",
        24 => "Too many open files",
        90 => "Message too long",
        93 => "Protocol not supported",
        97 => "Address family not supported by protocol",
        99 => "Cannot assign requested address",
        100 => "Network is down",
        101 => "Network is unreachable",
        105 => "No buffer space available",
        113 => "No route to host",
        _ => return format!("Unknown error {errno}"),
    };
    text.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strtoul_reads_like_the_c_library() {
        assert_eq!(strtoul(b"6801"), (6801, false));
        assert_eq!(strtoul(b" \t+42"), (42, false));
        assert_eq!(strtoul(b"12x"), (12, true));
        assert_eq!(strtoul(b"0x10"), (0, true));
        assert_eq!(strtoul(b""), (0, false));
        assert_eq!(strtoul(b"   "), (0, true));
        assert_eq!(strtoul(b"-"), (0, true));
        assert_eq!(strtoul(b"-0"), (0, false));
        assert_eq!(strtoul(b"-1"), (c_ulong::MAX, false));
        assert_eq!(strtoul(b"99999999999999999999999"), (c_ulong::MAX, false));
        assert_eq!(strtoul(b"99999999999999999999999 "), (c_ulong::MAX, true));
        assert_eq!(strtoul(b"5 "), (5, true));
    }

    #[test]
    #[cfg(target_pointer_width = "32")]
    fn a_negative_wraps_in_32_bits_on_the_head_unit() {
        // -(2^32 - 1801) is 1801 in a 32-bit unsigned long: the car's C worker takes --port -4294965495 as 1801.
        assert_eq!(strtoul(b"-4294965495"), (1801, false));
        assert_eq!(strtoul(b"4294967296"), (c_ulong::MAX, false));
    }

    #[test]
    fn ipv4_is_as_strict_as_inet_pton() {
        assert_eq!(ipv4(b"169.254.10.3"), Some(Ipv4Addr::new(169, 254, 10, 3)));
        assert_eq!(ipv4(b"0.0.0.0"), Some(Ipv4Addr::UNSPECIFIED));
        for bad in [
            &b"01.2.3.4"[..],
            b"1.2.3",
            b"1.2.3.4.",
            b"256.1.1.1",
            b"1.2.3.4 ",
            b" 1.2.3.4",
            b"1..2.3",
            b"",
            b"1.2.3.0004",
            b"1.2.3.-4",
            b"\xff",
        ] {
            assert_eq!(ipv4(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn errors_read_as_glibc_words_them() {
        assert_eq!(strerror(101), "Network is unreachable");
        assert_eq!(strerror(4242), "Unknown error 4242");
    }
}

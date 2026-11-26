//! ena-capture: records the frames a gauge worker sends, for tools/try_on_car.py, which runs a worker with --udp-port
//! set to this listener's port so that the apps' own ports (42860 and 42863, which iDrive holds) stay untouched.
//!   ena-capture [--seconds 1-600]
//! It binds a free UDP port on 127.0.0.1 and prints "PORT n", then one line per datagram: the CLOCK_MONOTONIC
//! millisecond it arrived (the clock the workers stamp their frames with) and its bytes in hex. It ends after the
//! seconds (60 unless given), on SIGTERM or on SIGINT, and exits 0; 2 for wrong arguments, 1 without a socket. It sends
//! nothing.

use ena_diag::{arguments, mono_ms, number, program_name, running, warn};
use std::io::Write;
use std::net::UdpSocket;
use std::time::Duration;

fn main() {
    let args = arguments();
    let seconds = match args.as_slice() {
        [_] => 60,
        [_, option, value] if option == b"--seconds" => number(value, 1, 600),
        _ => {
            warn(&[b"Usage: ", program_name(&args), b" [--seconds 1-600]\n"].concat());
            std::process::exit(2);
        }
    };
    ena_diag::handle_signals();
    let socket = match UdpSocket::bind("127.0.0.1:0") {
        Ok(socket) => socket,
        Err(error) => {
            warn(format!("bind 127.0.0.1: {error}\n").as_bytes());
            std::process::exit(1);
        }
    };
    let port = socket.local_addr().map(|a| a.port()).unwrap_or(0);
    // A short timeout, so that the deadline and a signal end the wait within a tenth of a second.
    let _ = socket.set_read_timeout(Some(Duration::from_millis(100)));
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "PORT {port}");
    let _ = out.flush();
    let deadline = mono_ms() + u64::from(seconds) * 1000;
    let mut buffer = [0u8; 2048];
    while running() && mono_ms() < deadline {
        let Ok(n) = socket.recv(&mut buffer) else {
            continue;
        };
        let mut line = format!("{} ", mono_ms());
        for byte in &buffer[..n] {
            line.push_str(&format!("{byte:02x}"));
        }
        line.push('\n');
        let _ = out.write_all(line.as_bytes());
        let _ = out.flush();
    }
}

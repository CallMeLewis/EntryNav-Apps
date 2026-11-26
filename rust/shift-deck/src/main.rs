//! shift-deck: ShiftDeck's worker (src/shift_deck.cpp). A port of src/collector.c's SHIFT_DECK_ONLY build that sends
//! the same frames and exits the same way. It reads OBD engine speed (01 0C) and road speed (01 0D) from the DME and
//! sends each reading to the app as a 16-byte drive frame (ena_diag::gauge::DriveFrame) on UDP 127.0.0.1:42863. It
//! only reads.
//!   shift-deck --host IPv4 [--port 6801] [--udp-port 42863] [--cycles 1-1000]
//! Engine speed on every pass, at most every 20 ms, so the bar follows the revs; road speed changes slowly and is read
//! every 65 ms, and the frames between carry the last one. It takes the diagnostic lease, gives the gateway up every
//! 500 ms or sooner when another worker asks, and ends when its parent changes (the app's process went). It prints
//! nothing. --search-dev, --search-port and --gateway-file are for the tests, as in the other workers. Exit 0 when it
//! stops, 2 for wrong arguments or without a parent, 1 without a UDP socket.

use ena_diag::gateway::GatewaySearch;
use ena_diag::gauge::{DriveFrame, FrameSocket, DRIVE_FRAME_SIZE, DRIVE_PORT};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway, should_yield, LEASE_MS, RETRY_MS, YIELD_MS};
use ena_diag::{arguments, mono_ms, number, parent_pid, pause_ms, running};
use std::os::fd::OwnedFd;

const DME: u8 = 0x12;
const ENGINE_SPEED: u8 = 0x0c;
const ROAD_SPEED: u8 = 0x0d;
const CYCLE_MS: u64 = 20;
const SPEED_MS: u64 = 65;
const IDLE_MS: i32 = 25;

/// One OBD mode 01 reading (read_live_pid): whether the connection is still good, and the value, -1 when the DME
/// refused or answered with the wrong length. width 1 is a byte; width 2 is a quarter-rpm count.
fn read_live_pid(fd: &OwnedFd, pid: u8, width: usize) -> (bool, i32) {
    let reply = exchange(fd, DME, &[0x01, pid], &[0x41, pid], 4, 250);
    let value = match &reply {
        Reply::Data(d) if d.len() == width + 2 && width == 1 => i32::from(d[2]),
        Reply::Data(d) if d.len() == width + 2 => ((u32::from(d[2]) * 256 + u32::from(d[3])) / 4) as i32,
        _ => -1,
    };
    (reply != Reply::Failed, value)
}

fn main() {
    let parent = parent_pid();
    let args = arguments();
    let mut host_option: Option<&[u8]> = None;
    let (mut port, mut udp_port, mut cycles) = (6801u16, DRIVE_PORT, 0u32);
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
            b"--udp-port" if has_value => {
                i += 1;
                udp_port = number(&args[i], 1, 65535) as u16;
            }
            b"--cycles" if has_value => {
                i += 1;
                cycles = number(&args[i], 1, 1000);
            }
            _ if search.option(&args, &mut i) => {}
            _ => std::process::exit(2),
        }
        i += 1;
    }
    // The C copies the host into 16 bytes (INET_ADDRSTRLEN).
    let Some(mut host) = host_option.filter(|h| h.len() < 16 && parent > 1).map(<[u8]>::to_vec) else {
        std::process::exit(2);
    };
    ena_diag::handle_signals();
    let Ok(socket) = FrameSocket::new() else {
        std::process::exit(1);
    };
    let (mut gateway, mut lock): (Option<OwnedFd>, Option<OwnedFd>) = (None, None);
    let (mut lease_until, mut retry_at, mut speed_due) = (0u64, 0u64, 0u64);
    let mut last_kph = -1;
    let mut count = 0u32;
    while running() && parent_pid() == parent && (cycles == 0 || count < cycles) {
        let start = mono_ms();
        if gateway.is_none() && start >= retry_at {
            lock = reserve_gateway().ok();
            if lock.is_some() {
                gateway = connect_gw(&host, port);
                if gateway.is_none() {
                    release_gateway(None, lock.take());
                    retry_at = mono_ms() + RETRY_MS;
                    if search.after_failure(&mut host, port) {
                        retry_at = 0;
                    }
                } else {
                    lease_until = mono_ms() + LEASE_MS;
                    search.failures = 0;
                    speed_due = 0; // a new connection reads the speed at once
                }
            }
        }
        if let Some(fd) = gateway.as_ref() {
            let (mut ok, rpm) = read_live_pid(fd, ENGINE_SPEED, 2);
            if ok && start >= speed_due {
                (ok, last_kph) = read_live_pid(fd, ROAD_SPEED, 1);
                speed_due = start + SPEED_MS;
            }
            if !ok {
                release_gateway(gateway.take(), lock.take());
                retry_at = mono_ms() + RETRY_MS;
            } else {
                let frame = DriveFrame {
                    sampled_ms: mono_ms(),
                    rpm,
                    kph: last_kph,
                };
                if socket.send(&frame.to_bytes(), udp_port) != DRIVE_FRAME_SIZE as isize {
                    break;
                }
                if lock
                    .as_ref()
                    .is_some_and(|lock| should_yield(lock, frame.sampled_ms, lease_until))
                {
                    release_gateway(gateway.take(), lock.take());
                    retry_at = frame.sampled_ms + YIELD_MS;
                }
            }
        }
        count = count.wrapping_add(1);
        let elapsed = mono_ms().wrapping_sub(start);
        if running() {
            pause_ms(if gateway.is_none() {
                IDLE_MS
            } else if elapsed < CYCLE_MS {
                (CYCLE_MS - elapsed) as i32
            } else {
                2
            });
        }
    }
    release_gateway(gateway, lock);
}

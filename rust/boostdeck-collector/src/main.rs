//! boostdeck-collector: BoostDeck's worker (src/boost_deck.cpp). A port of src/collector.c's default build that sends
//! the same frames, prints the same lines and exits the same way. It reads nine DIDs with UDS ReadDataByIdentifier
//! (22), eight from the DME (12) and the transmission temperature from the EGS (18), and sends the readings to the app
//! as a text frame (ena_diag::gauge::encode) on UDP 127.0.0.1:42860 after every pass. It only reads.
//!   boostdeck-collector (--host IPv4 | --demo) [--port 6801] [--udp-port 42860] [--cycles N] [--no-trans] [--trace]
//!                       [--session]
//! The app runs --host GATEWAY --session: then it takes the diagnostic lease, gives the gateway up every 500 ms or
//! sooner when another worker asks, and ends when its parent changes (the app's process went). Without --session it
//! connects at once and keeps the connection, for the tests. A pass is at most every 35 ms, 55 ms while a session has
//! no gateway. Boost, AFR, timing, rail and oil pressure are read on every pass; ambient pressure every second, and
//! the three temperatures every two seconds, staggered after the first pass. --no-trans leaves out the EGS, --trace
//! prints each answer on stderr, and --demo sends made-up readings without a car. --search-dev, --search-port and
//! --gateway-file are for the tests, as in the other workers. Exit 0 when it stops, 2 for wrong arguments, 1 without a
//! UDP socket.

use ena_diag::gateway::GatewaySearch;
use ena_diag::gauge::{decode_did, encode, fixed6, Frame, FrameSocket, COOLANT, COUNT, OIL, RAIL, UDP_PORT};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway, should_yield, LEASE_MS, RETRY_MS, YIELD_MS};
use ena_diag::{arguments, mono_ms, number, parent_pid, program_name, running, sleep_ms, strerror, warn};
use std::os::fd::OwnedFd;

/// The DIDs in the order a pass reads them: ambient pressure first (the boost reading needs it), then one per reading.
const DIDS: [u16; 9] = [0x4201, 0x4205, 0x5889, 0x4a49, 0x580f, 0xda12, 0x56d7, 0x586f, 0x4300];
/// How often each is read: 0 on every pass, else every so many ms, the first time this long after the first pass.
const PERIODS: [u64; 9] = [1000, 0, 0, 0, 2000, 2000, 0, 0, 2000];
const FIRST_DELAY: [u64; 9] = [900, 0, 0, 0, 500, 1150, 0, 0, 1700];
const EGS_DID: u16 = 0xda12;
const TICK_MS: u64 = 35;
const SESSION_IDLE_MS: u64 = 55;
const MINIMUM_IDLE_MS: u64 = 3;
/// A DID whose read lost the connection is left out this long, in a session.
const FAILED_DID_MS: u64 = 2000;
/// Boost needs an ambient pressure read this recently.
const AMBIENT_FRESH_MS: u64 = 1800;
const KPA_TO_PSI: f64 = 0.145037738;
const GASOLINE_AFR: f64 = 14.7;
const REPLY_LIMIT: usize = 4094;

/// What read_did found.
enum Read {
    /// The connection is gone or the ECU didn't answer in time.
    Lost,
    /// A refusal, or an answer that isn't a reading.
    Unavailable,
    Value(f64),
}

fn read_did(fd: &OwnedFd, ecu: u8, did: u16, trace: bool) -> Read {
    let [high, low] = did.to_be_bytes();
    let answer = match exchange(fd, ecu, &[0x22, high, low], &[0x62, high, low], REPLY_LIMIT, 500) {
        Reply::Refused(nrc) => {
            warn(format!("ECU {ecu:02X} DID {did:04X} NRC {nrc:02X}\n").as_bytes());
            return Read::Unavailable;
        }
        Reply::Failed => return Read::Lost,
        Reply::Data(answer) => answer,
    };
    let payload = &answer[3..];
    let decoded = decode_did(did, payload);
    if trace {
        let mut line = format!("RAW mono_ms={} ecu={ecu:02X} did={did:04X} payload=", mono_ms());
        for byte in payload {
            line.push_str(&format!("{byte:02X}"));
        }
        match decoded {
            Some(x) => line.push_str(&format!(" decoded={}\n", fixed6(x))),
            None if did == 0x4a49 && payload == [0xff, 0xff] => line.push_str(" unavailable reason=ambiguous-FFFF\n"),
            None => line.push_str(" unavailable reason=invalid-or-out-of-range\n"),
        }
        warn(line.as_bytes());
    }
    decoded.map_or(Read::Unavailable, Read::Value)
}

/// Stamps the frame and each reading's age, and sends it. False when it couldn't be sent.
fn send_frame(socket: &FrameSocket, port: u16, f: &mut Frame, observed: &[u64; COUNT]) -> bool {
    let now = mono_ms();
    f.sent_ms = now;
    for (age, seen) in f.age_ms.iter_mut().zip(observed) {
        *age = now.wrapping_sub(*seen).min(999999) as u32;
    }
    encode(f).is_some_and(|packet| socket.send(&packet, port) == packet.len() as isize)
}

fn usage(args: &[Vec<u8>]) -> ! {
    let mut text = b"Usage: ".to_vec();
    text.extend_from_slice(program_name(args));
    text.extend_from_slice(
        b" (--host IPv4 | --demo) [--port 6801] [--udp-port 42860] [--cycles N] [--no-trans] [--trace] [--session]\n",
    );
    warn(&text);
    std::process::exit(2);
}

fn main() {
    let parent = parent_pid();
    let args = arguments();
    let mut host_option: Option<&[u8]> = None;
    let (mut port, mut udp_port, mut cycles) = (6801u16, UDP_PORT, 0u32);
    let (mut demo, mut session, mut trace, mut no_trans) = (false, false, false, false);
    let mut search = GatewaySearch::default();
    let mut i = 1;
    while i < args.len() {
        let has_value = i + 1 < args.len();
        match args[i].as_slice() {
            b"--demo" => demo = true,
            b"--session" => session = true,
            b"--trace" => trace = true,
            b"--no-trans" => no_trans = true,
            _ if search.option(&args, &mut i) => {}
            b"--host" if has_value => {
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
                cycles = number(&args[i], 1, 1000000);
            }
            _ => usage(&args),
        }
        i += 1;
    }
    if host_option.is_some() == demo {
        warn(b"Choose --host or --demo, never both.\n");
        std::process::exit(2);
    }
    let mut host = Vec::new();
    if let Some(option) = host_option {
        // The C copies it into 16 bytes (INET_ADDRSTRLEN).
        if option.len() >= 16 {
            warn(&[b"Invalid host: ", option, b"\n"].concat());
            std::process::exit(2);
        }
        host = option.to_vec();
    }
    ena_diag::handle_signals();
    if session && (parent <= 1 || demo) {
        std::process::exit(2);
    }
    let socket = match FrameSocket::new() {
        Ok(socket) => socket,
        Err(errno) => {
            warn(format!("UDP socket: {}\n", strerror(errno)).as_bytes());
            std::process::exit(1);
        }
    };
    let mut f = Frame {
        demo: u32::from(demo),
        ..Frame::default()
    };
    warn(
        format!(
            "{}: 2016 340i candidate profile; validate against ISTA/logger.\n",
            if demo {
                "DEMO / NO VEHICLE CONNECTION"
            } else {
                "READ ONLY / HSFZ"
            }
        )
        .as_bytes(),
    );
    let mut observed = [0u64; COUNT];
    let (mut ambient, mut ambient_time, mut have_ambient) = (0.0f64, 0u64, false);
    let mut due = [0u64; DIDS.len()];
    let mut failed_until = [0u64; DIDS.len()];
    let (mut lease_until, mut retry_at) = (0u64, 0u64);
    let mut scanned = false;
    let (mut gateway, mut lock): (Option<OwnedFd>, Option<OwnedFd>) = (None, None);
    let mut count = 0u32;
    while running() && (cycles == 0 || count < cycles) && (!session || parent_pid() == parent) {
        let start = mono_ms();
        f.seq = f.seq.wrapping_add(1);
        if demo {
            let t = f64::from(count) * (TICK_MS as f64 / 1000.0);
            f.value[..5].copy_from_slice(&[
                12.0 + 8.0 * t.sin(),
                13.2 + 0.6 * t.sin(),
                8.0 + 3.0 * t.sin(),
                44.0 + 2.0 * t.sin(),
                88.0,
            ]);
            f.value[RAIL] = 95.0 + 22.0 * t.sin();
            f.value[OIL] = 2.5 + 0.5 * t.sin();
            f.value[COOLANT] = 86.0 + 3.0 * t.sin();
            f.valid = (1 << COUNT) - 1;
            for seen in observed.iter_mut() {
                *seen = mono_ms();
            }
        } else {
            let full_scan = if session { !scanned } else { gateway.is_none() };
            if gateway.is_none() && start >= retry_at {
                if session {
                    lock = reserve_gateway().ok();
                }
                if !session || lock.is_some() {
                    gateway = connect_gw(&host, port);
                    if gateway.is_none() && session {
                        release_gateway(None, lock.take());
                        retry_at = mono_ms() + RETRY_MS;
                    } else if session {
                        lease_until = mono_ms() + LEASE_MS;
                    }
                    if gateway.is_some() {
                        search.failures = 0;
                    } else if search.after_failure(&mut host, port) {
                        retry_at = 0;
                    }
                }
            }
            for i in 0..DIDS.len() {
                let Some(fd) = gateway.as_ref().filter(|_| running()) else {
                    break;
                };
                let did = DIDS[i];
                if no_trans && did == EGS_DID {
                    continue;
                }
                if session && start < failed_until[i] {
                    continue;
                }
                if !full_scan && PERIODS[i] != 0 && start < due[i] {
                    continue;
                }
                if full_scan {
                    due[i] = start + FIRST_DELAY[i];
                } else if PERIODS[i] != 0 {
                    due[i] += PERIODS[i];
                    if due[i] <= start {
                        due[i] = start + PERIODS[i];
                    }
                }
                if i != 0 {
                    f.valid &= !(1 << (i - 1));
                }
                let mut v = match read_did(fd, if did == EGS_DID { 0x18 } else { 0x12 }, did, trace) {
                    Read::Lost => {
                        release_gateway(gateway.take(), lock.take());
                        if session {
                            failed_until[i] = mono_ms() + FAILED_DID_MS;
                            retry_at = mono_ms() + RETRY_MS;
                        } else {
                            f.valid = 0;
                            have_ambient = false;
                        }
                        break;
                    }
                    Read::Unavailable => None,
                    Read::Value(v) => Some(v),
                };
                if i == 0 {
                    have_ambient = v.is_some();
                    if let Some(v) = v {
                        ambient = v;
                        ambient_time = mono_ms();
                    }
                    continue;
                }
                let Some(value) = v.as_mut() else {
                    continue;
                };
                if i == 1 {
                    if !have_ambient || mono_ms().wrapping_sub(ambient_time) > AMBIENT_FRESH_MS {
                        continue;
                    }
                    *value = (*value - ambient) * KPA_TO_PSI; // kPa absolute less ambient: gauge psi
                }
                if i == 2 {
                    *value *= GASOLINE_AFR; // measured lambda to gasoline-equivalent AFR
                }
                f.value[i - 1] = *value;
                observed[i - 1] = mono_ms();
                f.valid |= 1 << (i - 1);
            }
            if gateway.is_some() {
                scanned = true;
                if session
                    && lock
                        .as_ref()
                        .is_some_and(|lock| should_yield(lock, mono_ms(), lease_until))
                {
                    release_gateway(gateway.take(), lock.take());
                    retry_at = mono_ms() + YIELD_MS;
                }
            }
        }
        if !send_frame(&socket, udp_port, &mut f, &observed) {
            warn(format!("send frame: {}\n", strerror(ena_diag::errno())).as_bytes());
            ena_diag::end_run();
        }
        count = count.wrapping_add(1);
        let elapsed = mono_ms().wrapping_sub(start);
        let interval = if session && gateway.is_none() {
            SESSION_IDLE_MS
        } else {
            TICK_MS
        };
        let mut idle = interval.saturating_sub(elapsed);
        if !demo && idle < MINIMUM_IDLE_MS {
            idle = MINIMUM_IDLE_MS;
        }
        if idle > 0 {
            sleep_ms(idle as i32);
        }
    }
    release_gateway(gateway, lock);
}

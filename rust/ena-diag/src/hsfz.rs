//! HSFZ, BMW's diagnostics over TCP, to the gateway on port 6801, and one UDS request and its answer through it
//! (connect_gw, ready, transfer and exchange in src/collector.c).
//!
//! A frame is a 4-byte length, a 2-byte control word and the body. Diagnostic messages (control 1) carry the tester's
//! address F4, the ECU's address and the UDS bytes. The gateway's alive check (control 0x12) gets its answer (0x13)
//! at once, and a "response pending" refusal (7F xx 78) keeps waiting, within the same deadline.

use crate::{mono_ms, running, sys};
use std::os::fd::{AsRawFd, OwnedFd};

pub const TESTER: u8 = 0xf4;
const BODY_LIMIT: usize = 4096;
const UDS_LIMIT: usize = 64;

/// How a request ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Reply {
    /// The ECU's positive answer: the UDS bytes after its address and the tester's.
    Data(Vec<u8>),
    /// A negative response, with its code (NRC).
    Refused(u8),
    /// No answer within the time, a broken connection, or an answer larger than the caller takes.
    Failed,
}

/// Whether fd is ready for events before deadline. A signal that stops the run ends the wait.
pub(crate) fn ready(fd: &OwnedFd, events: i16, deadline: u64) -> bool {
    while running() {
        let now = mono_ms();
        if now >= deadline {
            return false;
        }
        let (rc, revents) = sys::poll_one(fd.as_raw_fd(), events, (deadline - now) as i32);
        if rc < 0 && sys::errno() == sys::EINTR {
            continue;
        }
        return rc > 0 && revents & events != 0;
    }
    false
}

/// Sends all of buffer, or fills it, before deadline.
fn transfer(fd: &OwnedFd, buffer: &mut [u8], sending: bool, deadline: u64) -> bool {
    let mut done = 0;
    while done < buffer.len() && running() {
        if !ready(fd, if sending { sys::POLLOUT } else { sys::POLLIN }, deadline) {
            return false;
        }
        let k = if sending {
            sys::send_bytes(fd, &buffer[done..])
        } else {
            sys::recv_bytes(fd, &mut buffer[done..])
        };
        if k < 0 && matches!(sys::errno(), sys::EINTR | sys::EAGAIN) {
            continue;
        }
        if k <= 0 {
            return false;
        }
        done += k as usize;
    }
    done == buffer.len()
}

/// A TCP connection to host:port within a second, non-blocking with Nagle off, or None.
pub fn connect_gw(host: &[u8], port: u16) -> Option<OwnedFd> {
    let address = crate::ipv4(host)?;
    let fd = sys::tcp_socket()?;
    sys::set_no_delay(&fd);
    if !sys::set_nonblocking(&fd) {
        return None;
    }
    if sys::connect_to(&fd, address, port) < 0 && sys::errno() != sys::EINPROGRESS {
        return None;
    }
    if !ready(&fd, sys::POLLOUT, mono_ms() + 1000) || sys::socket_error(&fd) != Some(0) {
        return None;
    }
    Some(fd)
}

/// The HSFZ frame of one UDS request from the tester to ecu.
pub fn request_frame(ecu: u8, uds: &[u8]) -> Vec<u8> {
    let mut frame = vec![0, 0, 0, (uds.len() + 2) as u8, 0, 1, TESTER, ecu];
    frame.extend_from_slice(uds);
    frame
}

/// The answer to the gateway's alive check.
pub const ALIVE_REPLY: [u8; 8] = [0, 0, 0, 2, 0, 0x13, 0, TESTER];

/// Sends uds to ecu and waits up to timeout_ms for the ECU's answer that starts with prefix. Other ECUs' messages,
/// answers to other requests and the gateway's acknowledgements are skipped; an answer longer than reply_limit
/// fails the request.
pub fn exchange(fd: &OwnedFd, ecu: u8, uds: &[u8], prefix: &[u8], reply_limit: usize, timeout_ms: u64) -> Reply {
    let deadline = mono_ms() + timeout_ms;
    if uds.len() > UDS_LIMIT {
        return Reply::Failed;
    }
    if !transfer(fd, &mut request_frame(ecu, uds), true, deadline) {
        return Reply::Failed;
    }
    let mut header = [0u8; 6];
    while running() {
        if !transfer(fd, &mut header, false, deadline) {
            return Reply::Failed;
        }
        let length = u32::from_be_bytes([header[0], header[1], header[2], header[3]]) as usize;
        let control = u16::from_be_bytes([header[4], header[5]]);
        if length > BODY_LIMIT {
            return Reply::Failed;
        }
        let mut body = vec![0u8; length];
        if !transfer(fd, &mut body, false, deadline) {
            return Reply::Failed;
        }
        if control == 0x12 {
            if !transfer(fd, &mut ALIVE_REPLY.clone(), true, deadline) {
                return Reply::Failed;
            }
            continue;
        }
        if control != 1 || length < 3 || body[0] != ecu || body[1] != TESTER {
            continue;
        }
        if length == 5 && body[2] == 0x7f && body[3] == uds[0] {
            if body[4] == 0x78 {
                continue; // response pending
            }
            return Reply::Refused(body[4]);
        }
        let answer = &body[2..];
        if answer.len() < prefix.len() || &answer[..prefix.len()] != prefix {
            continue;
        }
        if answer.len() > reply_limit {
            return Reply::Failed;
        }
        return Reply::Data(answer.to_vec());
    }
    Reply::Failed
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::thread;

    fn frame(control: u16, body: &[u8]) -> Vec<u8> {
        let mut out = (body.len() as u32).to_be_bytes().to_vec();
        out.extend_from_slice(&control.to_be_bytes());
        out.extend_from_slice(body);
        out
    }

    fn read_frame(conn: &mut TcpStream) -> (u16, Vec<u8>) {
        let mut header = [0u8; 6];
        conn.read_exact(&mut header).unwrap();
        let mut body = vec![0u8; u32::from_be_bytes(header[..4].try_into().unwrap()) as usize];
        conn.read_exact(&mut body).unwrap();
        (u16::from_be_bytes([header[4], header[5]]), body)
    }

    /// The frames the gateway read: control word and body.
    type Frames = Vec<(u16, Vec<u8>)>;

    /// A gateway that reads one request, then sends each of answers (raw bytes), each in two pieces.
    fn gateway(answers: Vec<Vec<u8>>) -> (u16, thread::JoinHandle<Frames>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            conn.set_nodelay(true).unwrap();
            let mut seen = vec![read_frame(&mut conn)];
            for answer in answers {
                let half = answer.len() / 2;
                conn.write_all(&answer[..half]).unwrap();
                conn.write_all(&answer[half..]).unwrap();
                if answer[4..6] == [0, 0x12] {
                    seen.push(read_frame(&mut conn));
                }
            }
            let mut rest = Vec::new();
            let _ = conn.read_to_end(&mut rest);
            seen
        });
        (port, handle)
    }

    fn ask(answers: Vec<Vec<u8>>, limit: usize, timeout: u64) -> (Reply, Frames) {
        let (port, handle) = gateway(answers);
        let fd = connect_gw(b"127.0.0.1", port).expect("connects");
        let reply = exchange(&fd, 0x12, &[0x19, 0x02, 0xaf], &[0x59, 0x02], limit, timeout);
        drop(fd);
        (reply, handle.join().unwrap())
    }

    #[test]
    fn a_request_is_one_frame_to_the_ecu() {
        assert_eq!(
            request_frame(0x12, &[0x01, 0x00]),
            [0, 0, 0, 4, 0, 1, 0xf4, 0x12, 0x01, 0x00]
        );
    }

    #[test]
    fn skips_acknowledgements_other_ecus_and_pending_then_takes_the_answer() {
        let answers = vec![
            frame(2, &[0xf4, 0x12, 0x19, 0x02, 0xaf]), // the gateway's acknowledgement
            frame(1, &[0x99, 0xf4, 0x59, 0x02, 0xff]), // another ECU
            frame(1, &[0x12, 0xf4, 0x7f, 0x19, 0x78]), // response pending
            frame(1, &[0x12, 0xf4, 0x62, 0xf1, 0x90]), // another request's answer
            frame(1, &[0x12, 0xf4, 0x59, 0x02, 0xff, 0x1a, 0x2f, 0x0f, 0x2f]),
        ];
        let (reply, seen) = ask(answers, 4094, 2000);
        assert_eq!(reply, Reply::Data(vec![0x59, 0x02, 0xff, 0x1a, 0x2f, 0x0f, 0x2f]));
        assert_eq!(seen, vec![(1, vec![0xf4, 0x12, 0x19, 0x02, 0xaf])]);
    }

    #[test]
    fn answers_the_alive_check() {
        let answers = vec![frame(0x12, &[0, 0xf4]), frame(1, &[0x12, 0xf4, 0x59, 0x02, 0xff])];
        let (reply, seen) = ask(answers, 4094, 2000);
        assert_eq!(reply, Reply::Data(vec![0x59, 0x02, 0xff]));
        assert_eq!(seen[1], (0x13, vec![0, 0xf4]));
    }

    #[test]
    fn a_refusal_carries_its_code() {
        let (reply, _) = ask(vec![frame(1, &[0x12, 0xf4, 0x7f, 0x19, 0x31])], 4094, 2000);
        assert_eq!(reply, Reply::Refused(0x31));
    }

    #[test]
    fn a_refusal_of_another_service_is_skipped() {
        let answers = vec![
            frame(1, &[0x12, 0xf4, 0x7f, 0x22, 0x31]),
            frame(1, &[0x12, 0xf4, 0x59, 0x02, 0x00]),
        ];
        assert_eq!(ask(answers, 4094, 2000).0, Reply::Data(vec![0x59, 0x02, 0x00]));
    }

    #[test]
    fn too_long_an_answer_fails() {
        assert_eq!(
            ask(vec![frame(1, &[0x12, 0xf4, 0x59, 0x02, 1, 2, 3])], 4, 2000).0,
            Reply::Failed
        );
    }

    #[test]
    fn an_oversized_frame_fails_at_once() {
        let mut header = 500_000u32.to_be_bytes().to_vec();
        header.extend_from_slice(&[0, 1]);
        let start = mono_ms();
        assert_eq!(ask(vec![header], 4094, 3000).0, Reply::Failed);
        assert!(mono_ms() - start < 1000);
    }

    #[test]
    fn no_answer_fails_at_the_deadline() {
        let start = mono_ms();
        assert_eq!(ask(vec![], 4094, 300).0, Reply::Failed);
        let waited = mono_ms() - start;
        assert!((300..1000).contains(&waited), "{waited} ms");
    }

    #[test]
    fn no_listener_is_no_connection() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert!(connect_gw(b"127.0.0.1", port).is_none());
        assert!(connect_gw(b"127.0.0.256", 6801).is_none());
    }
}

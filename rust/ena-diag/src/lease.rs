//! The diagnostic lease of src/diagnostic_lease.h: one worker at a time talks to the gateway. The holder keeps a UDP
//! socket bound to 127.0.0.1:42861. A worker that finds the port taken sends the holder one byte, which asks a gauge
//! worker to give the gateway up within its lease, and tries again later. The gateway connection must close before
//! the lock does.

use crate::sys;
use std::net::Ipv4Addr;
use std::os::fd::OwnedFd;

pub const DIAGNOSTIC_PORT: u16 = 42861;
/// How long a gauge worker holds the gateway before it gives it up and takes it again.
pub const LEASE_MS: u64 = 500;
/// How long it holds it at least, even when another worker is waiting.
pub const MIN_LEASE_MS: u64 = 140;
/// How long it leaves the gateway alone after giving it up.
pub const YIELD_MS: u64 = 165;
/// How long it waits before it tries again after the connection failed.
pub const RETRY_MS: u64 = 300;

/// The lock, or the errno that kept it: EADDRINUSE while another worker holds it.
pub fn reserve_gateway() -> Result<OwnedFd, i32> {
    let fd = sys::udp_socket().ok_or_else(sys::errno)?;
    if !sys::set_nonblocking(&fd) {
        return Err(sys::errno());
    }
    if sys::bind_to(&fd, Ipv4Addr::LOCALHOST, DIAGNOSTIC_PORT) < 0 {
        let saved = sys::errno();
        if saved == sys::EADDRINUSE {
            sys::send_to(&fd, &[1], Ipv4Addr::LOCALHOST, DIAGNOSTIC_PORT); // "waiting"
        }
        return Err(saved);
    }
    Ok(fd)
}

/// Closes the gateway connection, then gives up the lock.
pub fn release_gateway(connection: Option<OwnedFd>, lock: Option<OwnedFd>) {
    drop(connection);
    drop(lock);
}

/// Whether a gauge worker holding lock since until - LEASE_MS gives the gateway up now: once the lease is over, or
/// after MIN_LEASE_MS when a waiting worker's byte is there (gateway_should_yield, which reads one byte each time).
pub fn should_yield(lock: &OwnedFd, now: u64, until: u64) -> bool {
    if now >= until {
        return true;
    }
    until - now <= LEASE_MS - MIN_LEASE_MS && sys::recv_bytes(lock, &mut [0u8; 1]) > 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::UdpSocket;
    use std::time::Duration;

    #[test]
    fn a_waiting_worker_gets_the_gateway_after_the_shortest_lease() {
        // Any non-blocking UDP socket: should_yield only reads the waiting bytes from it.
        let lock = sys::udp_socket().unwrap();
        assert!(sys::set_nonblocking(&lock));
        assert_eq!(sys::bind_to(&lock, Ipv4Addr::LOCALHOST, 0), 0);
        let port = sys::local_port(&lock);
        let until = 10_000;
        let start = until - LEASE_MS;
        assert!(!should_yield(&lock, start, until), "nobody waits");
        UdpSocket::bind("127.0.0.1:0")
            .unwrap()
            .send_to(&[1], ("127.0.0.1", port))
            .unwrap();
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !should_yield(&lock, start + MIN_LEASE_MS - 1, until),
            "too soon to give it up"
        );
        assert!(
            should_yield(&lock, start + MIN_LEASE_MS, until),
            "the waiting byte, after 140 ms"
        );
        assert!(!should_yield(&lock, start + MIN_LEASE_MS, until), "the byte was read");
        assert!(should_yield(&lock, until, until), "the lease is over");
        assert!(should_yield(&lock, until + 1, until));
    }

    /// The one test that touches the real port, so that tests running side by side don't race for it.
    #[test]
    fn one_holder_at_a_time_and_a_waiting_worker_asks_for_it() {
        let lock = reserve_gateway().expect("the port is free");
        assert_eq!(reserve_gateway().err(), Some(sys::EADDRINUSE));
        release_gateway(None, Some(lock));
        let again = reserve_gateway().expect("free again once released");
        drop(again);

        // A holder that isn't this crate: the waiting worker's byte reaches it.
        let gauge = UdpSocket::bind(("127.0.0.1", DIAGNOSTIC_PORT)).unwrap();
        gauge.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        assert_eq!(reserve_gateway().err(), Some(sys::EADDRINUSE));
        let mut byte = [0u8; 4];
        assert_eq!(gauge.recv(&mut byte).unwrap(), 1);
        assert_eq!(byte[0], 1);
    }
}

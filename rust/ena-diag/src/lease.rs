//! The diagnostic lease of src/diagnostic_lease.h: one worker at a time talks to the gateway. The holder keeps a UDP
//! socket bound to 127.0.0.1:42861. A worker that finds the port taken sends the holder one byte, which asks a gauge
//! worker to give the gateway up within its lease, and tries again later. The gateway connection must close before
//! the lock does.

use crate::sys;
use std::net::Ipv4Addr;
use std::os::fd::OwnedFd;

pub const DIAGNOSTIC_PORT: u16 = 42861;

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

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::UdpSocket;
    use std::time::Duration;

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

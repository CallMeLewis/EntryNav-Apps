//! The C library calls the workers need that std doesn't offer in the same form: non-blocking sockets driven by poll,
//! a UDP socket that can broadcast and say who answered, the interface list and signal handlers. The functions,
//! structures and constants come from the libc crate, which has them right for each target. Every unsafe block in the
//! crate is here.

use std::ffi::CStr;
use std::io;
use std::mem;
use std::net::Ipv4Addr;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};

use libc::{c_int, sockaddr, sockaddr_in, socklen_t};

pub const EINTR: i32 = libc::EINTR;
pub const EAGAIN: i32 = libc::EAGAIN;
pub const EADDRINUSE: i32 = libc::EADDRINUSE;
pub const EINPROGRESS: i32 = libc::EINPROGRESS;

pub const POLLIN: i16 = libc::POLLIN;
pub const POLLOUT: i16 = libc::POLLOUT;

/// errno after the last failed call.
pub fn errno() -> i32 {
    io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

fn sockaddr(address: Ipv4Addr, port: u16) -> sockaddr_in {
    // SAFETY: sockaddr_in is plain data; all zeroes is a valid value, and the fields that matter are set below.
    let mut at: sockaddr_in = unsafe { mem::zeroed() };
    at.sin_family = libc::AF_INET as libc::sa_family_t;
    at.sin_port = port.to_be();
    at.sin_addr.s_addr = u32::from(address).to_be();
    at
}

const SOCKADDR_SIZE: socklen_t = mem::size_of::<sockaddr_in>() as socklen_t;

fn owned(fd: c_int) -> Option<OwnedFd> {
    // SAFETY: fd was just returned by socket() and nothing else owns it.
    (fd >= 0).then(|| unsafe { OwnedFd::from_raw_fd(fd) })
}

pub fn tcp_socket() -> Option<OwnedFd> {
    // SAFETY: plain call with constant arguments.
    owned(unsafe { libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0) })
}

pub fn udp_socket() -> Option<OwnedFd> {
    // SAFETY: plain call with constant arguments.
    owned(unsafe { libc::socket(libc::AF_INET, libc::SOCK_DGRAM, 0) })
}

fn set_int(fd: RawFd, level: c_int, name: c_int, value: c_int) -> bool {
    // SAFETY: value lives for the call and its size is passed.
    unsafe {
        libc::setsockopt(fd, level, name, (&value as *const c_int).cast(), mem::size_of::<c_int>() as socklen_t) == 0
    }
}

pub fn set_no_delay(fd: &OwnedFd) {
    set_int(fd.as_raw_fd(), libc::IPPROTO_TCP, libc::TCP_NODELAY, 1);
}

pub fn set_broadcast(fd: &OwnedFd) {
    set_int(fd.as_raw_fd(), libc::SOL_SOCKET, libc::SO_BROADCAST, 1);
}

/// fcntl(F_SETFL, O_NONBLOCK), as the C sets it.
pub fn set_nonblocking(fd: &OwnedFd) -> bool {
    // SAFETY: fcntl with an int third argument.
    unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) >= 0 }
}

/// connect(); 0, or -1 with errno.
pub fn connect_to(fd: &OwnedFd, address: Ipv4Addr, port: u16) -> i32 {
    let to = sockaddr(address, port);
    // SAFETY: to lives for the call and its size is passed.
    unsafe { libc::connect(fd.as_raw_fd(), (&to as *const sockaddr_in).cast::<sockaddr>(), SOCKADDR_SIZE) }
}

/// SO_ERROR, or None when getsockopt fails.
pub fn socket_error(fd: &OwnedFd) -> Option<i32> {
    let mut error: c_int = 0;
    let mut length = mem::size_of::<c_int>() as socklen_t;
    // SAFETY: error and length live for the call.
    let rc = unsafe {
        libc::getsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_ERROR,
            (&mut error as *mut c_int).cast(),
            &mut length,
        )
    };
    (rc == 0).then_some(error)
}

/// bind(); 0, or -1 with errno.
pub fn bind_to(fd: &OwnedFd, address: Ipv4Addr, port: u16) -> i32 {
    let at = sockaddr(address, port);
    // SAFETY: at lives for the call and its size is passed.
    unsafe { libc::bind(fd.as_raw_fd(), (&at as *const sockaddr_in).cast::<sockaddr>(), SOCKADDR_SIZE) }
}

pub fn send_bytes(fd: &OwnedFd, data: &[u8]) -> isize {
    // SAFETY: data is valid for its length.
    unsafe { libc::send(fd.as_raw_fd(), data.as_ptr().cast(), data.len(), 0) }
}

pub fn recv_bytes(fd: &OwnedFd, buffer: &mut [u8]) -> isize {
    // SAFETY: buffer is valid for its length.
    unsafe { libc::recv(fd.as_raw_fd(), buffer.as_mut_ptr().cast(), buffer.len(), 0) }
}

pub fn send_to(fd: &OwnedFd, data: &[u8], address: Ipv4Addr, port: u16) -> isize {
    let to = sockaddr(address, port);
    // SAFETY: data and to live for the call.
    unsafe {
        libc::sendto(
            fd.as_raw_fd(),
            data.as_ptr().cast(),
            data.len(),
            0,
            (&to as *const sockaddr_in).cast::<sockaddr>(),
            SOCKADDR_SIZE,
        )
    }
}

/// recvfrom(): the byte count (or -1) and the sender's IPv4 address.
pub fn recv_from(fd: &OwnedFd, buffer: &mut [u8]) -> (isize, Ipv4Addr) {
    let mut from = sockaddr(Ipv4Addr::UNSPECIFIED, 0);
    let mut length = SOCKADDR_SIZE;
    // SAFETY: buffer, from and length live for the call; from is big enough for an IPv4 sender, and the socket is
    // AF_INET so the kernel writes no other kind.
    let n = unsafe {
        libc::recvfrom(
            fd.as_raw_fd(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            0,
            (&mut from as *mut sockaddr_in).cast::<sockaddr>(),
            &mut length,
        )
    };
    (n, Ipv4Addr::from(u32::from_be(from.sin_addr.s_addr)))
}

/// poll() on one descriptor: the return value and revents.
pub fn poll_one(fd: RawFd, events: i16, timeout_ms: i32) -> (i32, i16) {
    let mut entry = libc::pollfd { fd, events, revents: 0 };
    // SAFETY: one entry, valid for the call.
    let rc = unsafe { libc::poll(&mut entry, 1, timeout_ms) };
    (rc, entry.revents)
}

/// poll(0, 0, ms): a sleep that a signal cuts short. -1 with errno when it was.
pub fn pause(timeout_ms: i32) -> i32 {
    // SAFETY: no descriptors.
    unsafe { libc::poll(std::ptr::null_mut(), 0, timeout_ms) }
}

/// One IPv4 address of an interface, in host order.
pub struct InterfaceAddress {
    pub name: Vec<u8>,
    pub address: u32,
    pub mask: u32,
}

/// The IPv4 address in [at], when it is one.
///
/// SAFETY: [at] is null or points to a socket address the C library filled in.
unsafe fn ipv4(at: *const sockaddr) -> Option<u32> {
    if at.is_null() || (*at).sa_family != libc::AF_INET as libc::sa_family_t {
        return None;
    }
    // An AF_INET address is a sockaddr_in.
    Some(u32::from_be((*at.cast::<sockaddr_in>()).sin_addr.s_addr))
}

/// getifaddrs(): every IPv4 address with a netmask, in the C library's order, or the errno it failed with.
pub fn interface_addresses() -> Result<Vec<InterfaceAddress>, i32> {
    let mut list: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs fills list, which freeifaddrs releases below; the entries are read only before that.
    unsafe {
        if libc::getifaddrs(&mut list) < 0 {
            return Err(errno());
        }
        let mut found = Vec::new();
        let mut entry = list;
        while !entry.is_null() {
            let item = &*entry;
            // As the C reads it: an IPv4 address with a netmask, the netmask read whatever its own family field says.
            if let Some(address) = ipv4(item.ifa_addr) {
                if !item.ifa_netmask.is_null() {
                    let mask = u32::from_be((*item.ifa_netmask.cast::<sockaddr_in>()).sin_addr.s_addr);
                    found.push(InterfaceAddress { name: CStr::from_ptr(item.ifa_name).to_bytes().to_vec(), address, mask });
                }
            }
            entry = item.ifa_next;
        }
        libc::freeifaddrs(list);
        Ok(found)
    }
}

/// SIGINT and SIGTERM call handler; SIGPIPE is ignored. signal() keeps SA_RESTART, as the car's glibc does.
pub fn handle_stop_signals(handler: extern "C" fn(c_int)) {
    // SAFETY: handler only stores to an atomic, which is async-signal-safe.
    unsafe {
        libc::signal(libc::SIGINT, handler as libc::sighandler_t);
        libc::signal(libc::SIGTERM, handler as libc::sighandler_t);
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }
}

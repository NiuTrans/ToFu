//! Tofu's optional post-connect physical write budget. No read timer, task,
//! polling loop or protocol interpretation. Connection/TLS metadata delegates
//! unchanged; the same socket owner receives and closes on the typed I/O error.
use super::{BoxConn, Connected, Connection, Read, ReadBufCursor, Write};
use futures_util::task::AtomicWaker;
use hyper_util::client::legacy::connect::{capture_connection, CaptureConnection};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::{
    future::Future,
    io,
    io::IoSlice,
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use tokio::time::Sleep;

/// Captured from the exact pooled connection selected for a request. H2 can
/// replace its writer's original error with BrokenPipe in a sibling future.
#[derive(Clone)]
pub(crate) struct WriteTimeoutState(Arc<ConnectionState>);
struct ConnectionState {
    expired: AtomicBool,
    cancelled: AtomicBool,
    has_write_budget: bool,
    read_waker: AtomicWaker,
    write_waker: AtomicWaker,
}
impl WriteTimeoutState {
    pub(crate) fn expired(&self) -> bool {
        self.0.expired.load(Ordering::Acquire)
    }
    pub(crate) fn has_write_budget(&self) -> bool {
        self.0.has_write_budget
    }
    fn cancel(&self) {
        self.0.cancelled.store(true, Ordering::Release);
        self.0.read_waker.wake();
        self.0.write_waker.wake();
    }
    fn observe_cancel(&self, cx: &Context<'_>, read: bool) -> io::Result<()> {
        let waker = if read {
            &self.0.read_waker
        } else {
            &self.0.write_waker
        };
        waker.register(cx.waker());
        if self.0.cancelled.load(Ordering::Acquire) {
            Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "incomplete request upload cancelled",
            ))
        } else {
            Ok(())
        }
    }
}

/// A cancelled HTTP/1 send must not leave its already-buffered body uploading
/// in Hyper's connection task. H2 owns cancellation per stream; never abort its
/// entire shared connection for one caller's Stop.
pub(crate) struct UploadGuard {
    capture: CaptureConnection,
    response_received: bool,
}
impl UploadGuard {
    pub(crate) fn capture<B>(request: &mut http::Request<B>) -> Self {
        Self {
            capture: capture_connection(request),
            response_received: false,
        }
    }
    pub(crate) fn complete(&mut self) {
        self.response_received = true;
    }
    pub(crate) fn write_timed_out(&self) -> bool {
        if let Some(connection) = self.capture.connection_metadata().as_ref() {
            let mut extra = http::Extensions::new();
            connection.get_extras(&mut extra);
            return matches!(extra.get::<WriteTimeoutState>(), Some(state) if state.expired());
        }
        false
    }
}
impl Drop for UploadGuard {
    fn drop(&mut self) {
        if self.response_received {
            return;
        }
        if let Some(connection) = self.capture.connection_metadata().as_ref() {
            if connection.is_negotiated_h2() {
                return;
            }
            connection.poison();
            let mut extra = http::Extensions::new();
            connection.get_extras(&mut extra);
            if let Some(state) = extra.get::<WriteTimeoutState>() {
                state.cancel();
            }
        }
    }
}

struct WriteClock {
    limit: Option<Duration>,
    pending: Option<Pin<Box<Sleep>>>,
    expired: bool,
    state: WriteTimeoutState,
}

impl WriteClock {
    fn new(limit: Option<Duration>, state: WriteTimeoutState) -> Self {
        Self {
            limit,
            pending: None,
            expired: false,
            state,
        }
    }

    fn poll<T>(
        &mut self,
        cx: &mut Context<'_>,
        operation: impl FnOnce(&mut Context<'_>) -> Poll<io::Result<T>>,
    ) -> Poll<io::Result<T>> {
        let Some(limit) = self.limit else {
            return operation(cx);
        };
        if self.expired {
            return Poll::Ready(Err(io::Error::new(
                io::ErrorKind::TimedOut,
                crate::error::WriteTimedOut,
            )));
        }
        match operation(cx) {
            Poll::Ready(result) => {
                // Drop the timer registration: an idle reusable connection has
                // no pending timer and never wakes just to observe inactivity.
                self.pending = None;
                Poll::Ready(result)
            }
            Poll::Pending => {
                let timer = self
                    .pending
                    .get_or_insert_with(|| Box::pin(tokio::time::sleep(limit)));
                if timer.as_mut().poll(cx).is_ready() {
                    self.pending = None;
                    self.expired = true;
                    self.state.0.expired.store(true, Ordering::Release);
                    Poll::Ready(Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        crate::error::WriteTimedOut,
                    )))
                } else {
                    Poll::Pending
                }
            }
        }
    }
}

pub(super) struct WriteTimeoutIo {
    inner: BoxConn,
    write: WriteClock,
    flush: WriteClock,
    state: WriteTimeoutState,
}

impl WriteTimeoutIo {
    pub(super) fn new(inner: BoxConn, limit: Option<Duration>) -> Self {
        let state = WriteTimeoutState(Arc::new(ConnectionState {
            expired: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            has_write_budget: limit.is_some(),
            read_waker: AtomicWaker::new(),
            write_waker: AtomicWaker::new(),
        }));
        Self {
            inner,
            write: WriteClock::new(limit, state.clone()),
            flush: WriteClock::new(limit, state.clone()),
            state,
        }
    }
}

impl Connection for WriteTimeoutIo {
    fn connected(&self) -> Connected {
        self.inner.connected().extra(self.state.clone())
    }
}

#[cfg(feature = "__tls")]
impl super::TlsInfoFactory for WriteTimeoutIo {
    fn tls_info(&self) -> Option<crate::tls::TlsInfo> {
        self.inner.tls_info()
    }
}

impl Read for WriteTimeoutIo {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        this.state.observe_cancel(cx, true)?;
        Pin::new(&mut this.inner).poll_read(cx, buf)
    }
}

impl Write for WriteTimeoutIo {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        this.state.observe_cancel(cx, false)?;
        this.write
            .poll(cx, |cx| Pin::new(&mut this.inner).poll_write(cx, buf))
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        this.state.observe_cancel(cx, false)?;
        this.write.poll(cx, |cx| {
            Pin::new(&mut this.inner).poll_write_vectored(cx, bufs)
        })
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        this.state.observe_cancel(cx, false)?;
        // A ready flush must not cancel a different pending write clock.
        this.flush
            .poll(cx, |cx| Pin::new(&mut this.inner).poll_flush(cx))
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        this.state.observe_cancel(cx, false)?;
        this.flush
            .poll(cx, |cx| Pin::new(&mut this.inner).poll_shutdown(cx))
    }
}

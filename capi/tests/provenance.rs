//! The C callback's opaque context arrives intact: the callback reads
//! and writes through the caller's own pointer.
use b3_servil::*;
use core::ffi::c_void;

#[repr(align(64))]
struct Input([u8; 64]);

struct Context {
    expected: Digest,
    calls: usize,
}

unsafe extern "C" fn completed(context: *mut c_void, places: *mut Digest, count: usize) {
    assert_eq!(count, 1);
    let context = context.cast::<Context>();
    // Both pointers came from the caller and remain live through finish.
    // Access through the original raw pointers; no competing owner borrow.
    unsafe {
        assert_eq!(*places, (*context).expected);
        (*context).calls += 1;
    }
}

#[test]
fn callback_context_reaches_the_callback() {
    let input = Input([42; 64]);
    let mode = b3_mode_plain();
    let expected = unsafe { b3_hash(&mode, input.0.as_ptr().cast(), input.0.len()) };
    let context = Box::into_raw(Box::new(Context { expected, calls: 0 }));
    let mut output = Digest { bytes: [0; 32] };
    unsafe {
        let stream = b3_stream_begin(&mode, Some(completed), context.cast());
        b3_stream_submit(stream, input.0.as_ptr().cast(), input.0.len(), &mut output);
        b3_stream_finish(stream);
        assert_eq!((*context).calls, 1);
        assert_eq!(output, expected);
        drop(Box::from_raw(context));
    }
}

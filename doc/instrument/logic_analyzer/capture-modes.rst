Trigger, storage and capture modes
==================================

Trigger
-------

Value match (default)
~~~~~~~~~~~~~~~~~~~~~

The trigger compares the current sample of its own vector against a
value and a mask, both set by the host at arm time. Masked-out signals are
don't-care; an all-zero mask matches every sample, which is how you say
"start capturing now".

Nothing about the condition is fixed in the gateware: the width and the names
are, the condition itself is programmed for each capture.

Edge match
~~~~~~~~~~

.. code-block:: yaml

   trigger:
     capabilities: edge

The edge trigger compares two cycles at once — the previous sample and the
current one — each with its own value and mask. That covers a plain level
match, a rising or falling edge on any signal, and arbitrary old→new
transitions (for instance "the bus went from ``INCR`` to ``FIXED`` while
``valid`` stayed high").

It costs one extra register stage of latency, which the core accounts for, so
the sample sitting at the trigger marker is the one in which the condition
became true either way.

The trigger vector is limited to 32 signals in both flavours. The probe
vector is not — see :doc:`signals` on splitting the two.

Capture window
--------------

Post-trigger
~~~~~~~~~~~~

The default: on the trigger, the core records ``count`` samples and stops.

Pre-trigger
~~~~~~~~~~~

Ask for a pre-trigger length and the buffer runs as a ring while armed, so
the capture holds the requested number of samples from *before* the trigger
and the rest after it. The core only enables the trigger once the pre-trigger
region is actually full, so a trigger firing immediately after arming still
yields a complete window.

In the trace, the trigger sample is at index 0 and pre-trigger samples are
numbered negatively.

Segmented capture (multiple windows)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   capture:
     max_windows: 8

With several windows, one arming captures several trigger events: the core
fills a window, re-arms itself, and repeats until the last window, laying
them back-to-back in the buffer. This is what you want for a rare event that
happens in bursts, or to compare successive occurrences of the same
transaction, without spending buffer on the dead time between them.

``max_windows`` is the maximum; the host picks how many to use for a given
capture, and ``count × windows`` must fit the buffer depth. Multi-window
capture is driven from the GUI and from the Python API; the command line
captures a single window.

Run-length encoding
~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   storage:
     rle: true

With RLE, the core stores a memory line when the signals change and a repeat
count while they do not. A signal set that idles for a million cycles then
does something interesting costs two lines instead of a million samples,
which is what makes a slow protocol observable at full clock rate.

The trade-offs:

* The number of samples a buffer holds is no longer fixed — it depends on how
  much the signals move. The host reports live progress (elapsed time and
  buffer fill) while capturing.
* A capture is bounded by a post-trigger time cap expressed in seconds, or
  simply runs until the buffer fills.
* Pre-trigger works, sized in buffer lines rather than samples.
* RLE is mutually exclusive with byte-lane packing and with multi-window
  capture.
* The run-count field is 32 bits, capped by the probe count, so a stretch of
  any length you are likely to wait through fits one line.

Trace storage
-------------

How a sample is laid out in the trace memory follows from the probe count;
you only choose whether to pack:

Plain
   One sample per memory word. This is the case when the probe vector is
   close to a word wide.

Packed (``storage.packed: true``)
   A sample narrower than a word is rounded up to 1, 2 or 4 bytes and several
   samples share a word, so the same block RAM holds two or four times as
   many samples. Worth setting whenever you probe 16 signals or fewer.

Wide (automatic)
   A probe vector wider than a memory word makes each sample span a run of
   words. This is what lifts any cap on the number of probes; only the bits
   you actually declared cost memory, the padding reads back as zero.

``storage.buffer_depth_l2`` counts samples in every case (encoded lines, under
RLE); the storage mode changes what a sample costs, not how many the buffer
holds. That key is what you tune against the memory available on the
device.

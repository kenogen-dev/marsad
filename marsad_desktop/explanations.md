# Marsad window: what every setting means

One file holds every explanation the window shows, so it can be read and edited in one place.

How the window reads it:

- Each explanation starts with `## key`. The key is what the code asks for; do not rename it.
- `tip:` is the one-line hover tip. It is always shown.
- `locks:` says whether the setting locks comparability. Use `yes`, `no` or `n/a`. The window turns it
  into a sentence.
- `default:` (optional) names a default whose source the window looks up and shows.
- Everything after those lines is the longer explanation, opened by the ⓘ mark. It is written for
  someone who has never used a radio program. Short paragraphs; **bold** and `code` work.
- Lines starting with `#` (one hash) are section headings for people reading this file. The window
  ignores them.

Words used throughout, defined once:

- **Radio**: the USB dongle (an SDR, "software-defined radio") that listens.
- **Scan / survey**: the radio steps across a range of frequencies again and again, writing how much
  power it hears at each one.
- **dB**: a ratio on a log scale. +3 dB is about twice the power and +10 dB ten times. Marsad's levels
  are *relative* dB: good for comparing, never a calibrated power.

# Starting a scan: run settings

## tag
tip: A short name of your own, put into the run's file names.
locks: no

A word or two that says what this run is for, such as `roof-test` or `after-new-cable`. It goes
into the names of the recording and its report, so you can find the run again later.

Only letters, numbers and dashes are safe. Leave it empty and the run is named after its preset
and the time.

## duration
tip: How long the radio records.
locks: no

How long the scan runs, from start to stop. The radio sweeps the whole range again and again for
this long.

Longer runs catch rarer signals and give a steadier noise floor, but make bigger files. Typical
values are 5 to 10 minutes for a quick check, 1 hour for a survey, and overnight for a
day-and-night picture.

The **warm-up** (below) is part of this time, not added to it. The line under the settings shows
how the time splits.

## duration_unit
tip: Minutes or hours. Switching converts the number, so the length stays the same.
locks: no

Choose minutes or hours for the duration. Switching converts the value: 0.5 hours becomes 30 min,
and 30 min becomes 0.5 hours. The length of the run does not change.

## radio
tip: Which radio makes this scan. It is found by its serial number when the scan starts.
locks: yes

The radio that does the scanning. A scan uses one radio; the other stays free.

Radios are found by **serial number**, never by their place in the USB list, because Windows
renumbers USB devices whenever one is unplugged.

Different radios hear differently: different tuners, ranges and blind spots. So a run on one radio
can only be compared with runs on the same radio. That is why the radio is part of the
fingerprint.

## analyze
tip: Make the report (detections, floor, pictures) as soon as recording ends.
locks: no

When the recording finishes, Marsad analyses it: it finds the noise floor, lists the signals above
it, tags the radio's own known spurs, and draws the pictures. The result is in the run's report
folder.

Turn this off only when you will analyse later with `marsad analyze`. Recording is the same either
way.

## warmup
tip: Minutes at the start of the run that the analysis leaves out while the radio settles.
locks: no
default: warmup

A radio drifts while it warms up: its noise level creeps for the first minutes, sometimes for an
hour. Measurements from that time are not like the rest of the run.

**Discard warm-up** tells the analysis to leave out the first part of the recording. The recording
itself is not lengthened and nothing is deleted; the analysis simply starts later. The minutes are
rounded up to whole sweeps, and the analysis always keeps at least the last sweep.

Typical: 10 to 20 minutes, or more for a radio known to drift for longer. If the warm-up is as long
as the run, nothing useful is analysed, and the panel says so.

## threshold
tip: How far above the noise floor something must be to count as a signal (dB).
locks: no
default: threshold

The **noise floor** is the level the radio hears when nothing is there: the hiss of its own
electronics and of the sky. A **signal** is anything that stands out above it.

The threshold is how far above the floor a frequency must reach to be listed as a detection.
Typical: 10 dB. Lower values catch weaker signals but also list random noise peaks; higher values
list only strong, certain signals.

It only changes the report, never the recording, so it can be changed later by analysing again.

## floor_window
tip: How wide a slice the analysis uses to work out the noise floor (MHz). Auto uses the preset's.
locks: no

The analysis works out the noise floor at each frequency from its neighbours: it takes a running
middle value over a window of frequencies, so single signals do not lift the floor.

The window must be much narrower than the scan. Over a few MHz, use well under 1 MHz. **Auto** uses
the preset's value, or 2 MHz.

Too wide a window on a narrow scan stops being a floor; too narrow, and a wide signal raises its own
floor and hides itself.

## chain_text
tip: The rig this run records: every part from the antenna to the PC, in order.
locks: no

Every run writes down the **chain**: the parts the signal passes through, from antenna or load to
the PC. It comes from the rig drawn in the Chain tab for this radio.

A run must describe its own rig. Months later the chain is what tells you whether two runs had the
same hardware.

## chain_override
tip: Type a different chain for this run only, such as a 50 ohm load or a pad.
locks: no

For a one-off change to the rig, such as a 50 ohm load in place of the antenna or an attenuator
added, type the chain for this run here. The saved rig in the Chain tab is not changed.

## checks
tip: What is checked before a start: the radio is free, found by serial, and the rig is confirmed.
locks: n/a

Before a scan starts, Marsad checks that:

- no other radio program (such as SDR++) is holding a radio, because only one program can use a
  radio at a time;
- the radio is plugged in, found by its serial number;
- the chain to record is known, and, if it was already used by an earlier run, that the rig is
  still built as drawn.

A red line stops the start. An amber line can be confirmed and started anyway.

# Conditions

## room
tip: What you note about the room and weather where you are. Recorded as text, not measured.
locks: no

These answers go into the run's record as plain text, to help explain a run later: was it a humid
day, was the window open, was a new appliance on?

They are filled in with your last answers. **Same as last time** puts those answers back after you
have changed them.

## room_temp
tip: The temperature you read, in °C (optional).
locks: no

The temperature where you measured it, usually the room. Leave it empty if you did not read one.

## room_humidity
tip: The humidity you read, in % (optional).
locks: no

The relative humidity, if you have a meter. Leave it empty if not.

## room_where
tip: Where the temperature and humidity were read: inside or outside.
locks: no

Says whether the two readings above are from inside the room or from outside.

## room_sky
tip: The sky as you saw it.
locks: no

Clear, overcast, rain and so on, as you see it. The automatic weather (below) adds measured
figures; this is your own note.

## room_notes
tip: Anything else: what is switched on, what changed, anything unusual.
locks: no

Anything that could matter to the radio: which devices are on, a new cable, someone using the
microwave, a storm. One line is enough.

## outdoor
tip: Outdoor weather and the sun's height are added to every run automatically.
locks: no

At the start and end of every run, Marsad records the outdoor weather (temperature, humidity,
pressure, cloud, rain, wind) and the sun's elevation and phase (day, twilight, night). This needs
the station's location, which stays private in its own file and is never shown here.

If there is no internet, or no location has been set, the run carries on normally and its record
says why the weather is missing. Nothing here can stop a run.

## same_as_last
tip: Put back the conditions you gave last time.
locks: n/a

Fills the room fields with the answers from your last start, after you have changed them here.

# Setting up a scan: the range

## start_from
tip: Begin from a station preset, then change what you need.
locks: n/a

A **preset** is a ready-made set of scan settings kept in the station's settings file, such as a
whole band at standard settings. Choosing one fills in every setting; you can then change any of
them.

## range_start
tip: The lowest frequency the scan covers (MHz).
locks: yes

The bottom of the range to scan, in MHz (megahertz, millions of cycles a second). The radio steps
from here up to the stop frequency.

## range_stop
tip: The highest frequency the scan covers (MHz).
locks: yes

The top of the range to scan. The program may stop a little short of it so the steps fit the range
exactly; the figures below show the real coverage.

## receiver
tip: Which radio this scan is set up for. Its range and blind spots are drawn above.
locks: yes

Choose the radio first: its tuning range and blind spots are drawn on the top strip, and its gain
steps fill the Gain list.

## target
tip: Optional: a frequency you are interested in. It is marked, and warned about if a hop centre lands on it.
locks: no

If you are after one frequency, type it here. It is marked on the strips, the hop strip opens zoomed
around it, and you are warned if a **hop centre** lands on it (some radios have a false spike at the
centre of every step, which could hide or imitate your signal).

Leave it at **none** for a general survey. It does not change what is recorded.

## hop_strip
tip: The tuner, the scan's hops, and one hop. Wheel or the zoom buttons zoom into the hop row.
locks: n/a

A radio hears only a slice of frequency at once, about 2 to 3 MHz. To cover a wider range it steps:
each step is a **hop**. The strips show:

1. **Tuner**: everything this radio can tune, with its blind spots hatched, and your range.
2. **Scan**: your range, divided into hops. Zoom in with the wheel or the buttons to see each hop's
   edges, centre, bins, cropped edges, the spike at its centre and the overlap with its neighbours.
3. **One hop**: its **bins** (the narrow slices each hop is cut into), the cropped edges, and the
   centre.

Drag on the top strip to choose a range.

# Setting up a scan: recording settings

## engine
tip: The program that drives the radio: rtl_power, or rtl_power_fftw for its extras.
locks: yes

Two programs can make a scan:

- **rtl_power**: the standard. It reaches up to about 2147 MHz.
- **rtl_power_fftw**: it can go above that, use much narrower bins, and subtract a baseline.

Their levels are on different scales, so a run from one is never comparable with a run from the
other.

## gain
tip: How much the radio amplifies before it digitises (dB). Only the radio's own steps are allowed.
locks: yes

**Gain** is amplification inside the radio, before the signal is turned into numbers. More gain
lifts weak signals above the radio's own digital noise. Too much lets strong signals overload it,
which creates false signals and hides real ones.

Each tuner has a fixed list of gain steps, so only those are offered. Gain changes every level in
the run, so runs at different gains are never compared directly.

## bin
tip: The width of each frequency slice the scan reports (kHz).
locks: yes

Each hop is cut into narrow slices called **bins**, and the scan reports the power in each one.
Narrower bins separate signals that are close together and show narrow signals more clearly, but
make bigger files.

The program rounds your request to a size it can make: the "Actual bin" figure is what you will
get. Typical: 10 to 100 kHz for surveys.

## integration
tip: How often the scan writes one complete sweep (seconds). Longer listens more per sweep, with fewer sweeps.
locks: yes

The **integration** (`-i`) is the time for one full **sweep** across the range. Within it, the
radio visits every hop, listens, and averages what it hears.

Every hop costs a fixed time to retune, whatever the integration. So a longer integration spends a
larger share of the time actually listening, but gives fewer sweeps in the run. Sweeps matter: the
noise floor and how often each frequency is busy are worked out from them.

Typical: 10 to 30 s for short checks, 120 s for long surveys. The trade-off table shows other
choices.

## fir
tip: The size of the smoothing filter applied before the FFT (rtl_power -F). Normally 9.
locks: yes

A smoothing filter rtl_power applies to the samples before cutting them into bins. It reduces
leakage of strong signals into neighbouring bins at the hop edges.

Keep the standard value (9) unless there is a reason. Changing it changes the levels slightly, so
the run will have nothing to compare with.

## crop
tip: The share of each hop's edges thrown away, because the edges are weak and unreliable.
locks: yes

The radio is weaker near the edges of each hop, so every hop is slightly wider than needed and its
edges are thrown away. **Crop** is the share discarded: 0.25 keeps the middle 75 %.

Keep the standard value (0.25). It is the station's convention, and changing it leaves the run with
nothing to compare with.

## fft_bins
tip: How many bins each hop is cut into (rtl_power_fftw).
locks: yes

For rtl_power_fftw: how many bins each hop is cut into. More bins means narrower bins. The bin
width is the sample rate divided by this number.

## seconds_per_hop
tip: How long rtl_power_fftw listens at each hop (seconds).
locks: yes

For rtl_power_fftw: how long it stays on each hop before moving on. Longer gives a steadier reading
of each hop but slower passes over the range.

## sample_rate
tip: How many samples a second the radio takes (MS/s). It sets how wide one hop is.
locks: yes

The **sample rate** is how many readings per second the radio sends to the PC. It sets how wide one
hop is: about the same number of MHz as MS/s.

It also changes the level the radio reports, so runs at different sample rates are never compared.

## overlap
tip: How much neighbouring hops overlap (%), so the edges cut off by crop are still covered.
locks: yes

For rtl_power_fftw: how much each hop overlaps its neighbour. The overlapped edges are the parts the
crop throws away, so with an overlap equal to the crop every frequency is still covered once.

## fingerprint
tip: The settings that must match for two runs to be compared. A change starts a new comparison set.
locks: yes

The **fingerprint** is a one-line summary of everything that changes the levels a run reports:
range, bin, gain, integration, crop, filter, program and radio.

Two runs can be compared directly only if their fingerprints match. Change any of these settings
and the run starts a new **comparison set**: it can be compared with later runs that use the same
new settings, but not with older ones.

## new_set
tip: These settings differ from the ones you started from, so this run cannot be compared with those runs.
locks: yes

You changed a setting that is part of the fingerprint. That is fine when it is a deliberate choice,
but the run will not be comparable with runs made with the old settings.

## timeline
tip: How the run's time splits: warm-up left out, then the part that is analysed.
locks: n/a

The run records for its whole duration. If warm-up is discarded, the first part is left out of the
analysis and the rest is analysed. The bar shows the split to scale, worked out by the engine in
whole sweeps.

## what_changed
tip: Which figures your last change moved, and why.
locks: n/a

Each time you change a setting, the figures that moved are listed here with the setting that moved
them, for example "sweeps 120 → 60: integration 30 s → 60 s".

## warnings
tip: Things about these settings that are worth knowing before you start.
locks: n/a

Warnings come from the same rules the engine uses. Red ones are problems you must confirm before
starting; amber ones are worth a look; plain ones are information.

## tradeoff
tip: Other integration choices and what each would give. Click a row to use it.
locks: yes

Each row is the same scan with a different integration (or seconds per hop). Longer settings listen
a larger share of the time but make fewer sweeps; shorter ones the reverse. Rows marked ✕ cannot
finish a sweep in time. Click a row to adopt it.

# What the program will do (figures from the engine)

## cell_hops
tip: How many steps the radio takes to cover the range.
locks: n/a

A **hop** is one step: the radio tunes to a frequency and listens to a slice about 2 to 3 MHz wide.
This is how many it takes to cover your range, and the width of each.

## cell_sample_rate
tip: The sample rate the program will actually use (MS/s).
locks: n/a

rtl_power picks the lowest sample rate that tiles your range exactly with whole hops. This is the
value it will use.

## cell_fft
tip: How many bins each hop is cut into, and how many are kept after crop.
locks: n/a

The **FFT** is the calculation that turns a hop's samples into bins. Its size is how many bins each
hop is cut into; the kept number is what remains after the crop removes the edges.

## cell_bin
tip: The bin width you will actually get (kHz).
locks: n/a

The width of each bin after the program has rounded your request. It may be narrower than you
asked, never wider.

## cell_bins
tip: How many bins each sweep writes.
locks: n/a

The number of frequency slices written per sweep, across the whole range.

## cell_sweeps
tip: How many complete sweeps the run will make, and how many remain after warm-up.
locks: n/a

A **sweep** is one full pass over the range. The floor and how often each frequency is busy are
worked out from the sweeps, so more sweeps give steadier figures.

## cell_listening
tip: The share of each sweep the radio spends listening rather than retuning.
locks: n/a
default: retune

Every hop costs a fixed retune time, in which the radio hears nothing. Listening is the share of
the sweep left for listening. It comes from the engine's timing model and the retune time per hop.

## cell_per_freq
tip: How long, over the whole run, the radio listens at any one frequency.
locks: n/a

Adds up the listening at one frequency across every sweep. Rare short signals are only caught while
the radio is listening at their frequency, so this is the figure that says how likely a short burst
is to be caught.

## cell_occupancy
tip: The smallest change in "how often busy" one sweep can show.
locks: n/a

**Occupancy** is the share of sweeps in which a frequency was above the threshold. With N analysed
sweeps the smallest step is 100/N %; more sweeps give finer steps.

## cell_file
tip: About how big the recording will be on disk.
locks: n/a

An estimate of the size of the spectrum file. Captured IQ from a recorder is separate and much
larger.

## cell_finishes
tip: The clock time the run will end.
locks: n/a

Now plus the duration.

## cell_passes
tip: How many passes over the range rtl_power_fftw will make.
locks: n/a

rtl_power_fftw's word for a sweep. It stops after the pass in which the duration runs out.

## cell_one_pass
tip: How long one pass takes, including the overhead at every hop.
locks: n/a
default: retune_fftw

The listening time at every hop plus the fixed overhead per hop (retune and first read).

## cell_spectra
tip: How many spectra are averaged at each hop.
locks: n/a

Each hop's listening is cut into blocks, each turned into a spectrum; their average is what is
written. More spectra give a steadier reading.

# Starting a recording

## record_mode_survey
tip: One radio scans, the other records the signals the first one finds. Needs two radios.
locks: n/a

**Survey + record** uses both radios. The **survey radio** scans the range as an ordinary scan.
Whenever it finds a signal over the trigger level, the **record radio** tunes to it and records the
raw radio data (**IQ**: the full waveform, which can be analysed in detail later).

It needs both radios plugged in and enabled. With one radio it is greyed out.

## record_mode_fixed
tip: One radio stays on one frequency and records every event there. Works with one radio.
locks: n/a

**Fixed frequency** parks one radio on one frequency. Each time something rises over the trigger
level, it is logged and, in an open band, recorded. This works with a single radio.

## saved_scan_or_preset
tip: The scan the survey radio runs: one of your saved scans or a station preset.
locks: yes

The survey part is an ordinary scan, so it uses the same settings and fingerprint as one. Choose a
saved scan or preset.

## surveyor
tip: The radio that scans.
locks: yes

The radio that does the survey. The survey records this radio's name, so its fingerprint matches its
other runs.

## recorder_radio
tip: The radio that records what the survey finds.
locks: n/a

The radio that jumps to each signal the survey finds and records it. It can only record what it can
tune: the coverage map shows where the two radios overlap.

## swap
tip: Exchange the two radios' jobs: the surveyor records and the recorder surveys.
locks: n/a

Exchanges which radio surveys and which records. It applies to recordings started afterwards;
nothing already running changes. It is remembered for next time.

Because the survey records which radio made it, a swap changes the survey's fingerprint: its runs
compare with that radio's other runs.

## fixed_radio
tip: The radio to park on the frequency.
locks: n/a

Which radio listens. Only enabled radios are offered.

## fixed_freq
tip: The frequency to listen on, such as 433.92M.
locks: n/a

The centre frequency to park on. Write it with M for MHz or k for kHz.

## fixed_width
tip: How wide a band around the frequency to watch. Empty: the radio's whole band.
locks: n/a

The width of the band watched around the frequency. Leave it empty to watch everything the radio
hears at once.

## fixed_gain
tip: The recording radio's gain (dB). Empty: the station's recorder gain for that radio.
locks: n/a

Gain for the recording radio. Too high and strong signals clip; the recorder lowers it by itself
when a capture touches the limits.

## keep_level
tip: Only keep a recording if the signal reaches this many dB over the floor. Empty: keep all.
locks: n/a
default: keep

When a recording starts, its first seconds are held in memory. They are written to disk only once
the signal reaches this level over the noise; if it never does, nothing is written. It saves space
on weak triggers. **Default** uses the station's setting (off when 0).

## capture
tip: How long each recording lasts (seconds). Empty: worked out from the signal, else 30 s.
locks: n/a

The length of each recording. Left at default, the recorder works it out from what it has learned
about the signal, or uses 30 s.

## trigger
tip: How far over the floor a signal must rise to start a recording (dB).
locks: n/a
default: trigger

The level that starts an event. Lower catches weaker signals but records more noise; higher
records fewer, stronger signals.

## cooldown
tip: After recording a signal, wait this long before recording it again (minutes).
locks: n/a

Stops one busy signal filling the disk: once a signal has been recorded, it is only logged, not
recorded, until the cooldown has passed.

## passes_trigger
tip: How many sweeps in a row a signal must be seen before it is recorded.
locks: n/a

A signal must appear in this many sweeps in a row before the recorder goes after it. 1 reacts
fastest; 2 ignores one-off flickers.

## ambush
tip: A signal this strong (dB) in a single sweep makes the recorder wait on it for a while.
locks: n/a

A very strong signal seen only once makes the record radio park on that frequency for a few minutes
and record it if it comes back.

## recorder_gain
tip: The recording radio's gain (dB). Empty: the station's setting.
locks: n/a

Gain for the record radio in survey + record. Its levels are its own, separate from the survey's.

## coverage_map
tip: What each radio can tune, its blind spots hatched, and the range where both overlap.
locks: n/a

Each row is one radio's tuning range. **Hatched** parts are blind spots (a **PLL gap**: a band some
tuners cannot lock onto). The last row is where both radios can tune: in survey + record, only
signals found there can be recorded. A dashed box marks the survey's range.

# Radios

## radios_mode
tip: What the station can do right now with the radios plugged in and enabled.
locks: n/a

Says in words what is possible: a plain scan with one radio, survey + record with two, or fixed
frequency recording with one. It is worked out from which radios Windows sees plugged in, by serial
number, without opening them.

## radio_switch
tip: On: offered for scans and recording. Off: never offered, keeps its role. Running work is never stopped.
locks: n/a

Switch a radio off to keep it out of new work, for example while it is lent to another program or
being tested. A switched-off radio:

- is greyed out and marked **disabled**;
- is not offered for a scan or a recording, and a start on it is refused;
- keeps its role, so switching it back on restores everything.

Switching a radio off never stops what it is already doing. It only stops new starts.

## roles
tip: Which radio scans and which records in survey + record.
locks: n/a

Only matters when both radios run together. A plain scan uses whichever radio you pick for it.

## presence
tip: Plugged in or not, read from Windows' device list by serial number. Nothing is opened.
locks: n/a

Marsad asks Windows which radios are connected, by serial number, without opening them. So the
check is safe while a radio is in use, and it is repeated every few seconds.

## radio_range
tip: The frequencies this radio can tune, less its blind spots.
locks: n/a
default: range

The lowest and highest frequency the radio can tune. Tuners vary from unit to unit and drift as they
warm, so a measured value is better than a datasheet one.

## pll_gap
tip: A band this tuner cannot lock onto. Nothing is heard there.
locks: n/a
default: gap

Some tuners have a band in the middle of their range where they cannot lock (a **PLL gap**). Scans
skip it, and nothing in it can be heard by that radio. It moves a little as the radio warms.

## detection_floor
tip: The smallest change between two runs that is believed to be real (dB).
locks: n/a
default: detection_floor

Two identical runs never read exactly the same. This is the smallest difference between two runs
that the comparison tools treat as real: larger when the hardware was handled between them, smaller
when nothing was touched.

## radio_notes
tip: What the station's notes say about this radio.
locks: n/a

Notes kept in the station's settings about this radio or its tuner: known quirks, ranges to avoid,
and where each applies.

## radio_chain
tip: The rig this radio's runs record, from the Chain tab.
locks: n/a

The chain text a run on this radio writes into its record, taken from this radio's path in the rig
drawn in the Chain tab.

# Live Scan

## start_scan
tip: Open Start a scan: saved scans first, a full new scan one step away.
locks: n/a

Opens the start panel. Scans start outside the window, so closing the window never stops one.

## start_recording
tip: Open Start recording: survey + record with two radios, or one radio on one frequency.
locks: n/a

Opens the recorder panel, kept separate from scans so setting up a scan stays simple.

## follow
tip: Follow the newest recording automatically, or stay on the one you chose.
locks: n/a

When on, the view jumps to each new recording as it starts. Turn it off to stay on a finished survey.

## sweep_height
tip: How many pixels each sweep takes in the waterfall.
locks: n/a

The **waterfall** shows sweeps as rows, newest at the top, with colour for level. Fit squeezes every
sweep in; larger heights show fewer sweeps more clearly.

## layer_noise
tip: Show each sweep's own noise floor in the waterfall.
locks: n/a

Shows the floor worked out for each sweep, so a floor that moves over time is visible.

## layer_signal
tip: Show only what stands above each sweep's floor by the threshold.
locks: n/a

Highlights power that is at least the threshold above that sweep's floor: the signals.

## layer_internal
tip: Mark the radio's own known lines and, when one matches, a terminated run's floor.
locks: n/a

Every radio makes some signals of its own (clock harmonics, a spike at every hop centre). These are
marked so they are not mistaken for discoveries. When a run with a 50 ohm load in place of the
antenna has the same fingerprint, its floor is shown too: what the radio hears with no antenna.

## signal_threshold
tip: The signal layer's level over each sweep's floor (dB).
locks: n/a

Changes only what the signal layer shows. It does not change the recording or its report.

## stop
tip: Stop the run: it finishes its sweep, keeps what it recorded, and analyses it.
locks: n/a

Asks the run to stop cleanly: the recorder finishes the sweep in progress, keeps everything recorded
so far, then analyses and archives it as usual.

# Chain screen: cables, chokes and the antenna's lead (relay 017)

## cable_coax
tip: Coax: shielded RF cable, written into the recorded text as its length and type, e.g. "3 ft RG316 coax".
locks: n/a

**Coax** is shielded radio-frequency cable: a centre wire inside an insulator inside a braided shield.
It carries the antenna's signal between parts. Every short or long piece of RF cable is Coax with a
length; anything particular (connector genders, an S-bend, the maker) goes in its note.

The recorded text says its length and type, such as "6 in coax" or "3 ft RG316 coax". The coax type
(RG316, RG58 and so on) is free text. In records made before November 2026, "jumper" means a short
cable.

## cable_usb
tip: USB cable: the radio's data and power lead, written as its length, e.g. "16 ft USB cable".
locks: n/a

A **USB cable** carries the radio's samples to the PC and the radio's power from the PC. It is written
into the recorded text as its length, such as "1 ft USB cable". Anything particular (USB3, M:M) goes
in its note.

## cable_note_in_text
tip: When ticked, this note is written into the recorded text in brackets after the cable.
locks: n/a

A cable's note is drawing only unless this is ticked. Ticked, it follows the cable in the recorded
text in brackets, after "with chokes" when the cable has any: "3 ft RG316 coax (with chokes, S-bend)".
Changing the recorded text means the next run records the rig as changed.

## choke
tip: A choke: a ferrite bead or loop on a cable. Drop it onto a cable; slide it along to where it sits.
locks: n/a

A **choke** is a ferrite bead, or a few turns of the cable through a ferrite ring, that stops the
outside of the cable's shield carrying noise. It sits on a cable, never on its own.

Drop one from the "On cables" list onto a cable, then drag the bead along the wire to show roughly
where it is. A cable with any chokes is written "(with choke)" or "(with chokes)" in the recorded text;
how many and where are drawing only. Removing a choke changes only the drawing and that word.

## antenna_lead
tip: The lead moulded into the antenna: its length, connector and note. Drawing and Output only.
locks: n/a

Some antennas come with a short cable moulded into them that cannot be unplugged, often called a
pigtail. It belongs to the **antenna**, not the chain, so it is set here: length, connector (such as
"SMA male") and a note. It shows on the drawing and in Output, and never reaches the recorded text.

## old_words
tip: This drawing still uses "Jumper" or "Pigtail". Nothing changes until you convert it.
locks: n/a

Cables are now **Coax** or **USB** with a length. A part or cable saved as a "Jumper" can be converted
to Coax or USB: its length is read from its name or note where it can be, and its note is kept in the
text. A "Pigtail" after an antenna can be merged into that antenna as its built-in lead.

Each conversion changes that rig's recorded text, so the next run records it as changed. Nothing is
converted until you choose to, and Undo puts it back. Past runs' records are never changed: in records
made before November 2026, "jumper" means a short cable.

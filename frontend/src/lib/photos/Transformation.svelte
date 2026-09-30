<script lang="ts">
	// One person, becoming themselves.
	//
	// The morph is the display: a face turning into the next year's face, made
	// once on the server and kept. Until it exists the newest portrait stands in
	// its place, because a face somebody recognises is a better wait than a grey
	// rectangle.
	//
	// The year is named as it goes. An animated image tells the page nothing
	// about where it is, so the page follows the same schedule the morph was
	// built to — three frames held on a year, ten crossing to the next, one on
	// the next — and names whichever year is nearest. It restarts with the loop,
	// so it cannot drift further than one pass.

	import { t } from '$lib/i18n';
	import { formatNumber, request } from '$lib/kit';
	import { cropOf, morphOf, portraitOf } from '$lib/opus';

	type Frame = { year: number; face: number; straightness: number };
	type Timing = { ms: number; hold: number; steps: number };

	let {
		person,
		born,
		cover
	}: {
		person: number;
		born?: string | null;
		/** the face already known for this person, so the row is never a grey
		 * rectangle: it is in hand before this component asks anything */
		cover?: number | null;
	} = $props();

	let frames = $state<Frame[]>([]);
	let timing = $state<Timing>({ ms: 60, hold: 3, steps: 10 });
	let ready = $state(false);
	let loading = $state(true);
	let failed = $state('');
	let seen = $state(false);
	let visible = $state(false);
	let reel = $state<HTMLElement | null>(null);
	let at = $state(0);

	// A list of people is two hundred of these, and each one that animates ticks
	// every sixty milliseconds. Only the ones on the screen are worth the tick.
	$effect(() => {
		if (!reel) return;
		const watch = new IntersectionObserver(
			(entries) => {
				visible = entries.some((e) => e.isIntersecting);
				if (visible) seen = true;
			},
			{ rootMargin: '300px' }
		);
		watch.observe(reel);
		return () => watch.disconnect();
	});

	$effect(() => {
		const id = person;
		if (!seen) return;
		loading = true;
		failed = '';
		ready = false;
		request<{ frames?: Frame[]; morph?: Timing }>(`/api/photos/people/${id}/transformation`, {}, {
			on: { 503: () => (failed = t('photos.transformFailed')) }
		}).then((d) => {
			loading = false;
			if (!d) return;
			frames = d.frames ?? [];
			if (d.morph) timing = d.morph;
			at = Math.max(0, (d.frames?.length ?? 1) - 1);
		});
	});

	/** Where the morph is, frame by frame, on the schedule it was built to. */
	const beat = $derived(timing.hold + timing.steps + 1);
	const total = $derived(
		frames.length > 1 ? beat * (frames.length - 1) + timing.hold : 0
	);

	$effect(() => {
		if (!ready || !visible || frames.length < 2) return;
		const started = performance.now();
		const tick = setInterval(() => {
			const frame = Math.floor(((performance.now() - started) / timing.ms) % total);
			const step = Math.min(Math.floor(frame / beat), frames.length - 2);
			// the year of the nearest keyframe: held on this one until the crossing
			// is half over, then already the next
			const into = frame - step * beat;
			at = into > timing.hold + timing.steps / 2 ? step + 1 : step;
		}, timing.ms);
		return () => clearInterval(tick);
	});

	const now = $derived(frames[Math.min(at, Math.max(frames.length - 1, 0))]);
	const age = $derived(born && now ? now.year - Number(born.slice(0, 4)) : null);
</script>

<div class="reel" bind:this={reel}>
	<div class="plate">
		<!-- Three layers, each replacing the one under it as it arrives. The face
		     the person list already carried is there from the first paint; the
		     portrait squares it up; the morph, when it exists, moves. A person of
		     one year never gets the third, and there is nothing missing about
		     that — one year has nothing to become. -->
		{#if cover}
			<img class="ground" src={cropOf(cover)} alt="" decoding="async" />
		{/if}
		{#if frames.length}
			<img
				class="over on"
				src={portraitOf(frames[frames.length - 1].face)}
				alt=""
				decoding="async"
			/>
		{/if}
		{#if frames.length > 1}
			<img
				class="over"
				class:on={ready}
				src={morphOf(person)}
				alt=""
				decoding="async"
				onload={() => (ready = true)}
				onerror={() => (failed = t('photos.transformFailed'))}
			/>
		{/if}
		{#if now}
			<span class="stamp">
				{now.year}{#if age !== null && age >= 0}<span class="age"
						>{t('photos.aged', { n: formatNumber(age) })}</span
					>{/if}
			</span>
		{/if}
		{#if seen && !loading && failed}
			<span class="making warn">{failed}</span>
		{:else if !ready && frames.length > 1}
			<span class="making">{t('photos.making')}</span>
		{/if}
	</div>
</div>

<style>
	.reel {
		width: 224px;
	}
	.plate {
		position: relative;
		width: 224px;
		height: 224px;
		border-radius: 8px;
		overflow: hidden;
		background: var(--surface-2);
		display: grid;
		place-items: center;
	}
	.plate img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.ground {
		filter: saturate(0.9);
	}
	.over {
		opacity: 0;
		transition: opacity 0.5s ease;
	}
	.over.on {
		opacity: 1;
	}
	@media (prefers-reduced-motion: reduce) {
		.over {
			transition: none;
		}
	}
	.stamp {
		position: absolute;
		left: 0;
		bottom: 0;
		padding: 0.2rem 0.5rem;
		background: var(--veil-thick);
		color: var(--on-picture);
		font-size: var(--fs-s);
		font-variant-numeric: tabular-nums;
		border-radius: 0 5px 0 0;
	}
	.age {
		margin-left: 0.4rem;
		opacity: 0.7;
	}
	/* said while the file is being made, because twenty seconds of nothing is
	   indistinguishable from something broken */
	.making.warn {
		color: var(--warn);
	}
	.making {
		position: absolute;
		right: 0;
		top: 0;
		padding: 0.15rem 0.45rem;
		background: var(--veil);
		color: var(--on-picture-muted);
		font-size: var(--fs-xs);
		border-radius: 0 0 0 5px;
	}
</style>

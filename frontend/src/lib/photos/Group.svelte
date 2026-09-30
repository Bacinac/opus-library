<script lang="ts">
	// One group, opened from wherever somebody was looking at it.
	//
	// The run view opens a group without ever having held a row about it, so the
	// window asks the group to describe itself and owns everything a person does
	// to it: name it, date the person, throw it away.

	import { untrack } from 'svelte';
	import PhotoViewer from '$lib/opus/PhotoViewer.svelte';
	import { t } from '$lib/i18n';
	import {
		ArmedButton,
		Button,
		Dialog,
		Picks,
		formatDate,
		formatNumber,
		json,
		plural,
		request
	} from '$lib/kit';
	import {
		aboutOf,
		cropOf,
		me
	} from '$lib/opus';
	import { known } from './known.svelte';
	import { regrouping, unlessRegrouping } from '$lib/opus/photos';

	type Face = {
		id: number;
		score: number;
		photo: string;
		taken_at: string | null;
		person: { id: number; name: string } | null;
		/** how well this face fits the group; the low ones are worth a second look */
		fit: number;
	};
	type Page = {
		faces_total: number;
		years: [number | null, number | null];
		person: { id: number; name: string } | null;
		faces: Face[];
		next: string | null;
	};
	type Refusal = { person: string; born_on: string; earliest_photograph: string };

	let {
		cluster,
		onclose,
		onchanged
	}: {
		cluster: number;
		onclose: () => void;
		/** the group was named, dated or thrown away — whoever opened it should look again */
		onchanged?: () => void;
	} = $props();

	let total = $state(0);
	let years = $state<[number | null, number | null]>([null, null]);
	let person = $state<{ id: number; name: string } | null>(null);
	let faces = $state<Face[]>([]);
	/** where the next page starts; null once there is none */
	let next = $state<string | null>(null);
	let started = $state(false);
	let loading = $state(false);
	let naming = $state('');
	let refusal = $state<Refusal | null>(null);
	let handedBack = $state(0);
	let sheet = $state<HTMLElement | null>(null);
	let scrolled = $state(0);
	let sheetHeight = $state(600);
	let sheetWidth = $state(900);
	// A group is rarely all wrong. One face in it belongs to whoever was standing
	// behind, and discarding the group to be rid of them loses the forty that
	// were right.
	let picked = $state<Set<number>>(new Set());
	// which known person each face leans towards, and the tally per person
	let leans = $state<Record<number, string>>({});
	let leaning = $state<{ person_id: number; name: string; faces: number }[]>([]);
	// the photograph a face was cut from, opened whole
	let looking = $state<Record<string, unknown> | null>(null);

	/** Take the proposal for one person as a selection, for a person to look over
	 * and correct. The name goes in with it, so confirming is one press. */
	function propose(who: { name: string }) {
		picked = new Set(
			Object.entries(leans)
				.filter(([, name]) => name === who.name)
				.map(([id]) => Number(id))
		);
		naming = who.name;
	}

	function pick(id: number) {
		const next = new Set(picked);
		if (!next.delete(id)) next.add(id);
		picked = next;
	}

	async function open(f: Face) {
		looking = await request<Record<string, unknown>>(aboutOf(f.photo));
	}

	/** Faces moved: the group's middle moved with them, and every page after the
	 * first was ordered around a middle that is no longer there. */
	function again() {
		faces = [];
		next = null;
		started = false;
		more();
	}

	async function dropFaces() {
		const gone = [...picked];
		if (!(await request('/api/photos/faces/discard', json({ faces: gone }), unlessRegrouping))) return;
		picked = new Set();
		onchanged?.();
		if (gone.length >= total) onclose();
		else again();
	}

	// Only the group being opened is watched. Everything below writes state that
	// loading also READS — `next` and `loading` decide whether to ask for another
	// page — so tracking it would make the effect trigger itself, forever.
	$effect(() => {
		const id = cluster;
		untrack(() => {
			total = 0;
			naming = '';
			refusal = null;
			handedBack = 0;
			picked = new Set();
			looking = null;
			leans = {};
			leaning = [];
			again();
			known.load();
			request<{ leanings: { face: number; name: string }[]; people: typeof leaning }>(
				`/api/photos/clusters/${id}/leanings`
			).then((d) => {
				if (!d || id !== cluster) return;
				leans = Object.fromEntries(d.leanings.map((l) => [l.face, l.name]));
				leaning = d.people;
			});
		});
	});

	async function more() {
		if (loading || (started && next === null)) return;
		loading = true;
		const id = cluster;
		const q = new URLSearchParams({ limit: '200' });
		if (next) q.set('after', next);
		const d = await request<Page>(`/api/photos/clusters/${id}?${q}`);
		loading = false;
		if (!d || id !== cluster) {
			started = true;
			next = null;
			return;
		}
		total = d.faces_total;
		years = d.years;
		person = d.person;
		const seen = new Set(faces.map((f) => f.id));
		faces = [...faces, ...d.faces.filter((f) => !seen.has(f.id))];
		next = d.next;
		started = true;
	}

	// Only what can be seen, plus a little. A crop is 256 px square, so every one
	// that exists costs a quarter of a megabyte decoded — a group of 1,255 would
	// be three hundred megabytes of bitmap for a screen that shows forty.
	// loading="lazy" does not help: it defers the fetch, not the element.
	const CELL = 92 + 8 + 14; // tile, gap, caption
	const cols = $derived(Math.max(1, Math.floor((sheetWidth + 8) / (92 + 8))));
	const firstRow = $derived(Math.max(0, Math.floor(scrolled / CELL) - 2));
	const lastRow = $derived(firstRow + Math.ceil(sheetHeight / CELL) + 5);
	const shown = $derived(faces.slice(firstRow * cols, (lastRow + 1) * cols));
	const padTop = $derived(firstRow * CELL);
	const padBottom = $derived(
		Math.max(0, Math.ceil(faces.length / cols) * CELL - (lastRow + 1) * CELL)
	);

	function onScroll() {
		if (!sheet) return;
		scrolled = sheet.scrollTop;
		sheetHeight = sheet.clientHeight;
		if (sheet.scrollTop + sheet.clientHeight >= sheet.scrollHeight - 600) more();
	}

	const span = $derived(
		years[0] === null ? '' : years[0] === years[1] ? `${years[0]}` : `${years[0]}–${years[1]}`
	);

	/** Give the group to somebody. The name lands on every face in it as well as
	 * on the group, which is what makes re-clustering safe.
	 *
	 * With faces picked it gives those instead, and they leave the group as they
	 * go — the case being two people grouped as one, where naming the group
	 * would hand half of it the wrong name. */
	async function give(name: string, force = false) {
		refusal = null;
		// every face picked is the group itself, and a group that stays a group is
		// worth more than the same faces loose
		const chosen = picked.size === total && faces.length === total ? [] : [...picked];
		const on = {
			409: (body: Record<string, unknown>) => {
				if (!regrouping(body)) refusal = body.detail as Refusal;
			}
		};
		const said = chosen.length
			? await request<{ person: { id: number; name: string } }>(
					'/api/photos/faces/person',
					json({ name, faces: chosen, ignore_dates: force }),
					{ on }
				)
			: await request<{ person: { id: number; name: string } }>(
					`/api/photos/clusters/${cluster}/person`,
					json({ name, ignore_dates: force }, 'PUT'),
					{ on }
				);
		if (!said) return;
		naming = '';
		picked = new Set();
		known.load(true);
		onchanged?.();
		if (!chosen.length) {
			person = said.person;
			return;
		}
		if (chosen.length >= total) onclose();
		else again();
	}

	/** A real date replaces the estimate, and every group already on that person
	 * is asked again whether it can be theirs. */
	async function setBorn(who: { id: number; name: string }, born: string) {
		const d = await request<{ born_on: string | null; handed_back: { cluster: number }[] }>(
			`/api/photos/people/${who.id}`,
			json({ born_on: born }, 'PUT'),
			unlessRegrouping
		);
		if (!d) return;
		handedBack = d.handed_back.length;
		known.amend(who.id, { born_on: d.born_on, born_source: 'typed' });
		if (d.handed_back.some((h) => h.cluster === cluster)) person = null;
		onchanged?.();
	}

	/** Throw the group away. Its faces go with it, or the next pass would build
	 * the same group out of the same faces. */
	async function discard() {
		if (!(await request(`/api/photos/clusters/${cluster}`, { method: 'DELETE' }, unlessRegrouping))) return;
		onchanged?.();
		onclose();
	}
</script>

<Dialog
	size="full"
	title={[plural(total, 'people.faces.one', 'people.faces.few', 'people.faces.many'), span]
		.filter(Boolean)
		.join(' · ')}
	subtitle={faces.length < total ? t('people.shown', { n: formatNumber(faces.length) }) : ''}
	{onclose}
	onescape={() => (picked.size ? (picked = new Set()) : onclose())}
>
	{#snippet actions()}
		{#if me.admin}
			<ArmedButton size="small" onconfirm={discard}>{t('people.discard')}</ArmedButton>
		{/if}
	{/snippet}

	<div class="sheet">
		{#if me.admin}
			<div class="name">
				{#if person}
					<strong>{person.name}</strong>
					<input
						class="born"
						type="date"
						title={t('people.bornAsk')}
						aria-label={t('people.bornAsk')}
						value={known.byId.get(person.id)?.born_on ?? ''}
						onchange={(e) => e.currentTarget.value && setBorn(person!, e.currentTarget.value)}
					/>
				{/if}
				<input
					class="who"
					list="known-people"
					bind:value={naming}
					placeholder={picked.size ? t('people.whoChosen') : person ? t('people.rename') : t('people.who')}
					aria-label={t('people.who')}
					onkeydown={(e) => e.key === 'Enter' && naming.trim() && give(naming)}
				/>
				<datalist id="known-people">
					{#each known.list as k (k.id)}<option value={k.name}></option>{/each}
				</datalist>
				<Button size="small" tone="accent" disabled={!naming.trim()} onclick={() => give(naming)}>
					{picked.size ? t('people.giveChosen') : t('people.give')}
				</Button>
			</div>

			{#if leaning.length}
				<div class="leans">
					<Picks
						picks={leaning.map((l) => ({ key: l.name, label: l.name, note: formatNumber(l.faces) }))}
						chosen={picked.size && leaning.some((l) => l.name === naming) ? [naming] : []}
						onpick={(name) => propose({ name })}
					/>
				</div>
			{/if}

			{#if handedBack}
				<p class="refusal">{t('people.handedBack', { n: formatNumber(handedBack) })}</p>
			{:else if refusal}
				<p class="refusal">
					{t('people.refused', {
						who: refusal.person,
						born: formatDate(refusal.born_on),
						first: formatDate(refusal.earliest_photograph)
					})}
					<Button size="small" onclick={() => give(naming || refusal!.person, true)}>
						{t('people.anyway')}
					</Button>
				</p>
			{:else if picked.size}
				<p class="picked">
					<span>{t('people.pickedFaces', { n: formatNumber(picked.size) })}</span>
					<Button size="small" onclick={() => (picked = new Set())}>{t('people.clearPick')}</Button>
					<ArmedButton size="small" onconfirm={dropFaces}>{t('people.dropFaces')}</ArmedButton>
				</p>
			{/if}
		{:else if person}
			<div class="name"><strong>{person.name}</strong></div>
		{/if}

		<div class="faces" bind:this={sheet} bind:clientWidth={sheetWidth} onscroll={onScroll}>
			<div class="pad" style:height="{padTop}px"></div>
			{#each shown as f (f.id)}
				<figure class:odd={f.fit < 0.35} class:chose={picked.has(f.id)}>
					<button class="cut" onclick={() => open(f)} title={t('people.openPhoto')} aria-label={t('people.openPhoto')}>
						<img src={cropOf(f.id)} alt="" loading="lazy" decoding="async" />
					</button>
					{#if me.admin}
						<button
							class="tick"
							class:on={picked.has(f.id)}
							aria-pressed={picked.has(f.id)}
							title={t('people.pick')}
							aria-label={t('people.pick')}
							onclick={() => pick(f.id)}
						>
							{picked.has(f.id) ? '✓' : ''}
						</button>
					{/if}
					<figcaption>
						{f.taken_at ? formatDate(f.taken_at) : ''}
						<span class="fit">{formatNumber(f.fit, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
					</figcaption>
				</figure>
			{/each}
			<div class="pad" style:height="{padBottom}px"></div>
			{#if next !== null}
				<div class="rest">
					<Button size="small" onclick={() => more()} disabled={loading}>
						{loading ? t('common.loading') : t('people.more')}
					</Button>
				</div>
			{/if}
		</div>
	</div>
</Dialog>

{#if looking}
	<PhotoViewer photo={looking as never} onclose={() => (looking = null)} />
{/if}

<style>
	.sheet {
		display: grid;
		grid-template-rows: auto auto auto 1fr;
		height: 100%;
		min-height: 0;
	}
	.name {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		flex-wrap: wrap;
		padding-bottom: 0.6rem;
	}
	.who {
		min-width: 16rem;
	}
	.born {
		font-size: var(--fs-s);
	}
	.leans {
		padding-bottom: 0.6rem;
	}
	.refusal,
	.picked {
		display: flex;
		gap: 0.6rem;
		align-items: center;
		margin: 0;
		padding-bottom: 0.6rem;
		font-size: var(--fs-s);
	}
	.refusal {
		color: var(--warn);
	}
	.faces {
		overflow: auto;
		min-height: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
		gap: 0.5rem;
		align-content: start;
	}
	/* spacers that keep the scrollbar honest while most rows do not exist */
	.pad,
	.rest {
		grid-column: 1 / -1;
	}
	.rest {
		display: flex;
		justify-content: center;
		margin: 0.6rem 0;
	}
	figure {
		margin: 0;
		position: relative;
	}
	figure.chose {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: 6px;
	}
	.cut {
		display: block;
		width: 100%;
		padding: 0;
		border: 0;
		background: none;
		cursor: zoom-in;
	}
	/* Always drawn. Behind :hover it was a control only somebody who already
	   knew it was there could find, and on a touch screen it did not exist. */
	.tick {
		position: absolute;
		right: 4px;
		top: 4px;
		width: 22px;
		height: 22px;
		padding: 0;
		line-height: 1;
		border: 1px solid var(--on-picture-muted);
		border-radius: 5px;
		background: var(--veil);
		color: var(--on-picture);
		font-size: var(--fs-s);
		cursor: pointer;
	}
	.tick.on {
		background: var(--accent);
		border-color: transparent;
	}
	figure img {
		width: 100%;
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: 4px;
		display: block;
	}
	.odd img {
		outline: 2px solid var(--warn);
		outline-offset: -2px;
	}
	figcaption {
		font-size: var(--fs-xs);
		color: var(--muted);
		text-align: center;
		font-variant-numeric: tabular-nums;
		padding-top: 0.15rem;
	}
	.fit {
		margin-left: 0.3rem;
		opacity: 0.6;
	}
</style>

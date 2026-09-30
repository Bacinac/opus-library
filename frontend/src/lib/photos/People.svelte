<script lang="ts">
	// One person, across the years.
	//
	// The clustering refuses to link across time on purpose: two sisters at the
	// same age defeat any similarity, and only their birthdays tell them apart.
	// That refusal is right for a machine and useless to a person, who wants to
	// see the child at four beside the same child at fourteen and say yes or no.
	//
	// So this crosses the windows the clustering would not, and proposes rather
	// than decides. A row is a claim; naming it is the answer.

	import { t } from '$lib/i18n';
	import {
		ArmedButton,
		Button,
		formatDate,
		formatNumber,
		json,
		plural,
		request,
		Tag
	} from '$lib/kit';
	import {
		cropOf,
		me
	} from '$lib/opus';
	import { known } from './known.svelte';
	import { unlessRegrouping } from '$lib/opus/photos';
	import Transformation from './Transformation.svelte';

	type Era = {
		year: number | null;
		/** the largest group of that year, opened when the tile is clicked */
		id: number;
		/** every group of that year: what a tick chooses and a discard removes */
		ids: number[];
		faces: number;
		cover: number | null;
		person: { id: number; name: string } | null;
	};
	type Life = {
		groups: Era[];
		faces: number;
		years: [number | null, number | null];
		person: { id: number; name: string } | null;
		/** worked out from the photographs unless somebody typed a date */
		born: number | null;
		certain: boolean;
		/** the days, not a span: these are facts about photographs */
		first: string | null;
		last: string | null;
	};

	let { onopen }: { onopen?: (clusterId: number) => void } = $props();

	const LIMIT = 200;

	let lives = $state<Life[]>([]);
	let total = $state(0);
	let loading = $state(true);
	// what is being typed and what was refused belong to a run of groups, not to
	// a place in the list: the list is read again after every change, and a name
	// kept by position lands on whichever run has moved into that place
	let naming = $state<Record<number, string>>({});
	let refused = $state<Record<number, number>>({});
	let handed = $state<Record<number, number>>({});
	let picked = $state<Set<number>>(new Set());
	// A name typed on a GROUP moves that group to somebody; a name typed here
	// corrects the person. They look alike and are opposites, so the second one
	// is only reachable by saying so
	let editing = $state<number | null>(null);

	const lifeKey = (life: Life) => life.groups[0].id;

	async function reload() {
		const got = await request<{ lives: Life[]; total: number }>(
			`/api/photos/people/suggested?limit=${LIMIT}`
		);
		loading = false;
		if (!got) return;
		lives = got.lives;
		total = got.total;
		naming = {};
		refused = {};
	}

	$effect(() => {
		known.load();
		reload();
	});

	/** Take the chosen groups off whoever they were given to. The faces stay —
	 * this is for a group that is plainly a real person, just not this one. */
	async function detach() {
		const done = await request('/api/photos/clusters/detach', json({ clusters: [...picked] }), unlessRegrouping);
		if (!done) return;
		picked = new Set();
		await reload();
	}

	/** Throw the chosen groups away, faces and all. A group comes back only by
	 * running the pass that found its faces. */
	async function discard() {
		const done = await request('/api/photos/clusters/discard', json({ clusters: [...picked] }), unlessRegrouping);
		if (!done) return;
		picked = new Set();
		await reload();
	}

	function takeFocus(node: HTMLInputElement) {
		node.focus();
		node.select();
	}

	async function rename(who: { id: number; name: string }, name: string) {
		const said = name.trim();
		editing = null;
		if (!said || said === who.name) return;
		if (!(await request(`/api/photos/people/${who.id}`, json({ name: said }, 'PUT'), unlessRegrouping))) return;
		await reload();
		await known.load(true);
	}

	/** A year is chosen whole. It stands for every group stored under it — the
	 * one that holds the year and the singletons that would not weld to it — and
	 * choosing the tile that hides them has to reach them. */
	function pickYear(era: Era) {
		const next = new Set(picked);
		const on = era.ids.every((id) => next.has(id));
		for (const id of era.ids) on ? next.delete(id) : next.add(id);
		picked = next;
	}

	/** A typed date replaces the estimate, and every group already on that person
	 * is asked again whether it can be theirs. */
	async function born(who: { id: number; name: string }, date: string) {
		const said = await request<{ born_on: string | null; handed_back: unknown[] }>(
			`/api/photos/people/${who.id}`,
			json({ born_on: date }, 'PUT'),
			unlessRegrouping
		);
		editing = null;
		if (!said) return;
		handed = { ...handed, [who.id]: said.handed_back.length };
		known.amend(who.id, { born_on: said.born_on, born_source: 'typed' });
		await reload();
	}

	async function kin(who: { id: number }, family: boolean) {
		const said = await request<{ family: boolean }>(
			`/api/photos/people/${who.id}`,
			json({ family }, 'PUT'),
			unlessRegrouping
		);
		if (said) known.amend(who.id, { family: said.family });
	}

	async function give(life: Life, force = false) {
		const key = lifeKey(life);
		const name = (naming[key] ?? '').trim();
		if (!name) return;
		const said = await request<{ person: { id: number; name: string }; given: number[]; refused: unknown[] }>(
			'/api/photos/people/lives',
			json({ clusters: life.groups.flatMap((g) => g.ids), name, ignore_dates: force }, 'PUT'),
			unlessRegrouping
		);
		if (!said) return;
		refused = { ...refused, [key]: said.refused.length };
		if (said.given.length) {
			lives = lives.map((l) => (lifeKey(l) === key ? { ...l, person: said.person } : l));
			naming = { ...naming, [key]: '' };
			await known.load(true);
		}
	}
</script>

{#snippet face(g: Era, era = false)}
	<button class="face" class:era onclick={() => onopen?.(g.id)} title={formatNumber(g.faces)}>
		{#if g.cover}
			<img src={cropOf(g.cover)} alt="" loading="lazy" decoding="async" />
		{:else}
			<div class="blank"></div>
		{/if}
		<span class="yr">{g.year}</span>
		<span class="cnt">{formatNumber(g.faces)}</span>
	</button>
{/snippet}

{#if loading}
	<p class="dim">{t('common.loading')}</p>
{:else}
	{#if picked.size && me.admin}
		<div class="selection">
			<span>{t('lives.picked', { n: formatNumber(picked.size) })}</span>
			<ArmedButton size="small" onconfirm={discard}>{t('people.discard')}</ArmedButton>
			<ArmedButton size="small" tone="quiet" title={t('people.detachWhy')} onconfirm={detach}>
				{t('people.detach')}
			</ArmedButton>
			<Button size="small" onclick={() => (picked = new Set())}>{t('people.clearPick')}</Button>
		</div>
	{/if}

	{#each lives as life (lifeKey(life))}
		{@const key = lifeKey(life)}
		{@const who = life.person ? known.byId.get(life.person.id) : undefined}
		<section class="life">
			<header>
				{#if life.person && me.admin && editing === life.person.id}
					<!-- focused when it appears rather than by the attribute, which
					     moves focus for a screen reader that never asked to go there -->
					<input
						class="rename"
						use:takeFocus
						value={life.person.name}
						aria-label={t('people.renamePerson')}
						onblur={(e) => rename(life.person!, e.currentTarget.value)}
						onkeydown={(e) => {
							if (e.key === 'Enter') e.currentTarget.blur();
							else if (e.key === 'Escape') editing = null;
						}}
					/>
				{:else if life.person && me.admin}
					<button class="asname" title={t('people.renamePerson')} onclick={() => (editing = life.person!.id)}>
						{life.person.name}
					</button>
				{:else if life.person}
					<strong>{life.person.name}</strong>
				{:else if me.admin}
					<input
						list="known-people-lives"
						placeholder={t('people.who')}
						aria-label={t('people.who')}
						value={naming[key] ?? ''}
						oninput={(e) => (naming = { ...naming, [key]: e.currentTarget.value })}
						onkeydown={(e) => e.key === 'Enter' && give(life)}
					/>
					<Button size="small" disabled={!(naming[key] ?? '').trim()} onclick={() => give(life)}>
						{t('lives.give')}
					</Button>
				{/if}
				{#if life.person}
					<Tag tone={life.certain ? 'busy' : 'quiet'} dashed={!life.certain}>
						<span class="star">✳</span>
						{#if me.admin && editing === -life.person.id}
							<input
								class="date"
								type="date"
								use:takeFocus
								value={who?.born_on ?? ''}
								aria-label={t('people.bornAsk')}
								onblur={() => (editing = null)}
								onchange={(e) => e.currentTarget.value && born(life.person!, e.currentTarget.value)}
							/>
						{:else if who?.born_source === 'contacts' && who.born_on}
							<!-- kept in the address book, and a field editable in two
							     places is two facts waiting to disagree -->
							<span class="fromBook" title={t('people.fromContacts')}>{formatDate(who.born_on)}</span>
						{:else if me.admin}
							<button class="asdate" onclick={() => (editing = -life.person!.id)}>
								{who?.born_on
									? formatDate(who.born_on)
									: life.born
										? t('lives.bornAbout', { year: String(life.born) })
										: t('people.bornAsk')}
							</button>
						{:else}
							{who?.born_on
								? formatDate(who.born_on)
								: life.born
									? t('lives.bornAbout', { year: String(life.born) })
									: ''}
						{/if}
					</Tag>
					{#if who?.roster}
						<Tag tone="busy" title={t('people.familyByAccount')}>{t('people.family')}</Tag>
					{:else if me.admin}
						<Tag
							tone={who?.family ? 'busy' : 'quiet'}
							dashed={!who?.family}
							title={t(who?.family ? 'people.familyOff' : 'people.familyOn')}
							onclick={() => kin(life.person!, !who?.family)}>{t('people.family')}</Tag
						>
					{:else if who?.family}
						<Tag tone="busy">{t('people.family')}</Tag>
					{/if}
					{#if handed[life.person.id]}
						<span class="warn">{t('people.handedBack', { n: formatNumber(handed[life.person.id]) })}</span>
					{/if}
				{:else if life.born}
					<Tag tone="quiet" dashed>
						<span class="star">✳</span>
						{t('lives.bornAbout', { year: String(life.born) })}
					</Tag>
				{/if}

				<!-- two facts about photographs, each said in words. A run of years
				     with a dash through it is a lifespan, and that is not what this
				     is about. -->
				<span class="shot">{t('lives.firstShot')} <b>{life.first ? formatDate(life.first) : '—'}</b></span>
				<span class="shot">{t('lives.lastShot')} <b>{life.last ? formatDate(life.last) : '—'}</b></span>
				<span class="dim">
					{plural(life.faces, 'people.faces.one', 'people.faces.few', 'people.faces.many')} ·
					{plural(life.groups.length, 'lives.periods.one', 'lives.periods.few', 'lives.periods.many')}
				</span>
				{#if refused[key] && me.admin}
					<span class="warn">
						{t('lives.refused', { n: formatNumber(refused[key]) })}
						<Button size="small" onclick={() => give(life, true)}>{t('people.anyway')}</Button>
					</span>
				{/if}
			</header>
			<div class="body">
				<div class="left">
					{#if life.person}
						<Transformation
							person={life.person.id}
							born={who?.born_on}
							cover={who?.cover ?? life.groups[life.groups.length - 1]?.cover}
						/>
					{:else}
						<!-- nobody has said who this is, so there is no person to ask for
						     a run of portraits; the years themselves are the next best -->
						<div class="run">
							{#each life.groups as g (g.year)}
								{@render face(g, true)}
							{/each}
						</div>
					{/if}
				</div>

				<!-- One tile per year. Every group stored under that year is behind
				     it: welding leaves a tail of ones and twos beside the group that
				     holds the year, and stored-as-they-are that tail is most of what
				     a well assembled life would show. -->
				<div class="groups">
					{#each life.groups as g (g.year)}
						{@const on = g.ids.every((id) => picked.has(id))}
						<div class="one" class:picked={on}>
							{@render face(g)}
							{#if me.admin}
								<button
									class="tick"
									class:on
									title={t('people.pick')}
									aria-label={t('people.pick')}
									aria-pressed={on}
									onclick={() => pickYear(g)}
								>
									{on ? '✓' : ''}
								</button>
							{/if}
						</div>
					{/each}
				</div>
			</div>
		</section>
	{/each}

	{#if total > lives.length}
		<p class="dim">{t('people.shownOf', { shown: formatNumber(lives.length), total: formatNumber(total) })}</p>
	{/if}

	<datalist id="known-people-lives">
		{#each known.list as k (k.id)}<option value={k.name}></option>{/each}
	</datalist>
{/if}

<style>
	.dim {
		color: var(--muted);
		font-size: var(--fs-s);
	}
	.life {
		margin-bottom: 1.4rem;
		padding-bottom: 1rem;
		border-bottom: 1px solid var(--border);
	}
	.life header {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		flex-wrap: wrap;
		margin-bottom: 0.5rem;
	}
	.asname {
		font: inherit;
		font-weight: 600;
		padding: 0;
		border: 0;
		border-bottom: 1px dashed transparent;
		background: none;
		color: var(--text);
		cursor: text;
	}
	.asname:hover {
		border-bottom-color: var(--border);
	}
	.rename {
		font-weight: 600;
		padding: 0.15rem 0.4rem;
	}
	.life header input:not(.rename):not(.date) {
		font-size: var(--fs-m);
		padding: 0.25rem 0.5rem;
		min-width: 13rem;
	}
	.date {
		font: inherit;
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
	}
	.star {
		opacity: 0.55;
		font-size: 0.9em;
	}
	.asdate {
		font: inherit;
		padding: 0;
		border: 0;
		border-bottom: 1px dashed transparent;
		background: none;
		color: inherit;
		cursor: text;
	}
	.asdate:hover {
		border-bottom-color: var(--border);
	}
	.fromBook {
		cursor: help;
	}
	/* The photographs, in the colour OPUS gives to photographs, so a date about
	   pictures is visibly about pictures and not about the person. */
	.shot {
		font-size: var(--fs-s);
		color: var(--muted);
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.shot b {
		font-weight: 500;
		color: var(--kind-photos);
	}
	.shot + .shot {
		padding-left: 0.6rem;
		border-left: 1px solid var(--border);
	}
	.warn {
		color: var(--warn);
		font-size: var(--fs-s);
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	/* the years on the left, every group on the right. Two different things to
	   look at: one is the person changing, the other is what the machine actually
	   holds and what gets chosen and thrown away */
	.body {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 1.2rem;
		align-items: start;
	}
	.left {
		min-width: 0;
	}
	@media (max-width: 900px) {
		.body {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	.run,
	.groups {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		align-content: start;
	}
	.one {
		position: relative;
		flex: none;
		width: 56px;
		height: 56px;
	}
	.one .yr,
	.one .cnt {
		font-size: var(--fs-2xs);
	}
	.one.picked {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
		border-radius: 7px;
	}
	.face {
		position: relative;
		display: block;
		width: 100%;
		height: 100%;
		padding: 0;
		border: 0;
		border-radius: 6px;
		overflow: hidden;
		cursor: pointer;
		background: none;
	}
	.face.era {
		flex: none;
		width: 72px;
		height: 72px;
	}
	/* on the tile itself, because a group is thrown away while looking at it and
	   not from a menu somewhere else */
	.tick {
		position: absolute;
		right: 2px;
		top: 2px;
		width: 18px;
		height: 18px;
		padding: 0;
		line-height: 1;
		border: 1px solid var(--on-picture-muted);
		border-radius: 4px;
		background: var(--veil);
		color: var(--on-picture);
		font-size: var(--fs-xs);
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.12s ease;
	}
	.one:hover .tick,
	.tick:focus-visible,
	.tick.on {
		opacity: 1;
	}
	.tick.on {
		background: var(--accent);
		border-color: transparent;
	}
	/* it follows the page, because the groups being chosen are all over it */
	.selection {
		position: sticky;
		top: var(--shell-header, 0px);
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 0.7rem;
		padding: 0.5rem 0.7rem;
		margin-bottom: 0.8rem;
		border-radius: 8px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		font-size: var(--fs-m);
	}
	.face img,
	.blank {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
		background: var(--surface-2);
	}
	.yr,
	.cnt {
		position: absolute;
		color: var(--on-picture);
		font-size: var(--fs-2xs);
		font-variant-numeric: tabular-nums;
		padding: 0.1rem 0.3rem;
		background: var(--veil-thick);
	}
	.yr {
		left: 0;
		bottom: 0;
		border-radius: 0 4px 0 0;
	}
	.cnt {
		right: 0;
		top: 0;
		border-radius: 0 0 0 4px;
	}
</style>

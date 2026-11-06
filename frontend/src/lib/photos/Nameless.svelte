<script lang="ts">
	// The groups nobody has named.
	//
	// Naming is the one step of the pass a machine cannot take: it groups every
	// face it finds, and it can extend a name only to somebody it can already
	// measure — a person who has faces and was photographed near enough in time.
	// A person it has never met has nothing to be matched against, and a name
	// with no faces (there is one) is unrecognisable by construction, however
	// many times the pass runs.
	//
	// So thirteen and a half thousand groups have no name, and almost all of them
	// never should: twelve thousand are one or two faces of somebody who walked
	// through the frame at a wedding. What is worth a screen is the other end —
	// somebody who turns up twenty times in a family archive is not a passer-by.
	// The one-offs still get a door, newest first: a new friend starts as one.

	import { t } from '#lib/i18n/index.js';
	import { Button, Latest, Picks, formatDate, formatNumber, request } from '#lib/kit/index.js';
	import { cropOf } from '#lib/opus/index.js';

	let { onopen, stamp = 0 }: { onopen?: (clusterId: number) => void; stamp?: number } = $props();

	type Group = {
		id: number;
		faces: number;
		/** the first and last year it has a face in */
		years: [number, number] | null;
		/** when its first photograph reached the library */
		first_seen: string;
		cover: number | null;
	};
	type Page = { clusters: Group[]; next: number | null };

	// Where a stranger stops being a stranger. Not a threshold anybody can
	// defend to the face, but the shape of the library is unambiguous either
	// side of it: below, thousands of one-offs; above, a hundred or so people.
	const OFTEN = [20, 10, 5];
	const ANY = 'any';
	const ONCE = 'once';

	let size = $state('10');
	let order = $state<'size' | 'new'>('size');
	let groups = $state<Group[] | null>(null);
	let next = $state<number | null>(null);
	let loading = $state(true);
	const asking = new Latest();

	function page(after: number) {
		const q = new URLSearchParams({
			named: 'false',
			limit: '300',
			after: String(after),
			sort: order,
			min_size: size === ANY || size === ONCE ? '1' : size
		});
		if (size === ONCE) q.set('max_size', '1');
		return request<Page>(`/api/photos/clusters?${q}`, {}, { latest: asking });
	}

	$effect(() => {
		void stamp;
		loading = true;
		page(0).then((got) => {
			if (got) {
				groups = got.clusters;
				next = got.next;
			}
			loading = false;
		});
	});

	async function more() {
		if (next === null || !groups) return;
		loading = true;
		const got = await page(next);
		if (got && groups) {
			const seen = new Set(groups.map((g) => g.id));
			groups = [...groups, ...got.clusters.filter((g) => !seen.has(g.id))];
			next = got.next;
		}
		loading = false;
	}

	const span = (g: Group) => {
		const [a, b] = g.years ?? [];
		if (!a) return '';
		return a === b ? String(a) : `${a}–${b}`;
	};
</script>

<div class="least">
	<span>{t('nameless.faces')}</span>
	<Picks
		picks={[
			{ key: ANY, label: t('nameless.any') },
			{ key: ONCE, label: t('nameless.once') },
			...[...OFTEN].reverse().map((n) => ({ key: String(n), label: `${formatNumber(n)}+` }))
		]}
		chosen={[size]}
		onpick={(key) => (size = key)}
	/>
	<span>{t('nameless.order')}</span>
	<Picks
		picks={[
			{ key: 'size', label: t('nameless.bySize') },
			{ key: 'new', label: t('nameless.byNew') }
		]}
		chosen={[order]}
		onpick={(key) => (order = key === 'new' ? 'new' : 'size')}
	/>
</div>

{#if !groups && loading}
	<p class="says">{t('common.loading')}</p>
{:else if groups && !groups.length}
	<p class="says">{t('nameless.none')}</p>
{:else if groups}
	<div class="wall">
		{#each groups as g (g.id)}
			<button class="one" onclick={() => onopen?.(g.id)}>
				{#if g.cover}
					<img src={cropOf(g.cover)} alt="" loading="lazy" decoding="async" />
				{:else}
					<span class="none">□</span>
				{/if}
				<span class="many">{formatNumber(g.faces)}</span>
				{#if order === 'new'}
					<span class="when">{formatDate(g.first_seen)}</span>
				{:else if span(g)}
					<span class="when">{span(g)}</span>
				{/if}
			</button>
		{/each}
	</div>
	{#if next !== null}
		<div class="rest">
			<Button size="small" onclick={more} disabled={loading}>
				{loading ? t('common.loading') : t('people.more')}
			</Button>
		</div>
	{/if}
{/if}

<style>
	.says {
		color: var(--muted);
		font-size: var(--fs-m);
		margin: 0 0 0.8rem;
	}
	.least {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 1rem;
		font-size: var(--fs-m);
		color: var(--muted);
	}
	.rest {
		display: flex;
		justify-content: center;
		margin-top: 1rem;
	}
	.wall {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(6.5rem, 1fr));
		gap: 0.7rem;
	}
	.one {
		display: grid;
		gap: 0.2rem;
		justify-items: center;
		padding: 0;
		border: 0;
		background: none;
		color: var(--text);
		cursor: pointer;
	}
	.one img,
	.none {
		width: 100%;
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: 50%;
		background: var(--surface);
		display: grid;
		place-items: center;
		color: var(--muted);
	}
	.one:hover img,
	.one:focus-visible img {
		outline: 3px solid var(--accent);
		outline-offset: 3px;
	}
	.many {
		font-size: var(--fs-m);
		font-variant-numeric: tabular-nums;
	}
	.when {
		font-size: var(--fs-s);
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
</style>

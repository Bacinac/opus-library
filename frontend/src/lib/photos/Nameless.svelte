<script lang="ts">
	// The groups nobody has named, largest first.
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

	import { t } from '$lib/i18n';
	import { Latest, Picks, formatNumber, request } from '$lib/kit';
	import { cropOf } from '$lib/opus';

	let { onopen }: { onopen?: (clusterId: number) => void } = $props();

	type Group = {
		id: number;
		faces: number;
		/** the first and last year it has a face in */
		years: [number, number] | null;
		cover: number | null;
	};

	// Where a stranger stops being a stranger. Not a threshold anybody can
	// defend to the face, but the shape of the library is unambiguous either
	// side of it: below, thousands of one-offs; above, a hundred or so people.
	const OFTEN = [20, 10, 5];

	let least = $state(10);
	let groups = $state<Group[] | null>(null);
	let loading = $state(true);
	const asking = new Latest();

	$effect(() => {
		const min = least;
		loading = true;
		request<{ clusters: Group[] }>(
			`/api/photos/clusters?named=false&min_size=${min}&limit=300`,
			{},
			{ latest: asking }
		).then((got) => {
			groups = got?.clusters ?? null;
			loading = false;
		});
	});

	const span = (g: Group) => {
		const [a, b] = g.years ?? [];
		if (!a) return '';
		return a === b ? String(a) : `${a}–${b}`;
	};
</script>

<div class="least">
	<span>{t('nameless.atLeast')}</span>
	<Picks
		picks={OFTEN.map((n) => ({ key: String(n), label: formatNumber(n) }))}
		chosen={[String(least)]}
		onpick={(key) => (least = Number(key))}
	/>
</div>

{#if loading}
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
				{#if span(g)}<span class="when">{span(g)}</span>{/if}
			</button>
		{/each}
	</div>
{/if}

<style>
	.says {
		color: var(--muted);
		font-size: var(--fs-m);
		margin: 0 0 0.8rem;
	}
	.least {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 1rem;
		font-size: var(--fs-m);
		color: var(--muted);
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

<script lang="ts">
	// Everywhere the archive has been.
	//
	// A list and not a map. This surface is where a place is found in order to be
	// corrected — the map belongs to the everyday surface, which is Player, and
	// two maps would be one too many.

	import { t } from '$lib/i18n';
	import { formatDate, formatNumber, request } from '$lib/kit';
	import { tileOf } from '$lib/opus';

	type Place = {
		place: string;
		country: string;
		photographs: number;
		at: { lat: number; lon: number } | null;
		first: string | null;
		last: string | null;
		typed: number;
		cover: string | null;
		cover_turn: number;
	};

	let { onopen }: { onopen?: (place: string) => void } = $props();

	let places = $state<Place[] | null>(null);
	let loading = $state(true);
	let looking = $state('');

	$effect(() => {
		request<Place[]>('/api/photos/places').then((got) => {
			places = got;
			loading = false;
		});
	});

	const shown = $derived(
		!places
			? []
			: looking.trim()
				? places.filter((p) =>
						(p.place + ' ' + p.country).toLowerCase().includes(looking.trim().toLowerCase())
					)
				: places
	);

	const span = (p: Place) =>
		!p.first ? '' : p.first === p.last ? formatDate(p.first) : `${formatDate(p.first)} — ${formatDate(p.last!)}`;
</script>

{#if loading}
	<p class="dim">{t('common.loading')}</p>
{:else if places && !places.length}
	<p class="dim">{t('photos.noPlaces')}</p>
{:else if places}
	<input class="sift" bind:value={looking} placeholder={t('photos.siftPlaces')} aria-label={t('photos.siftPlaces')} />
	<div class="wall">
		{#each shown as p (p.place + p.country)}
			<button class="card" onclick={() => onopen?.(p.place)}>
				{#if p.cover}
					<img src={tileOf(p.cover, p.cover_turn)} alt="" loading="lazy" decoding="async" />
				{:else}
					<div class="blank"></div>
				{/if}
				<span class="name">{p.place}</span>
				<span class="under">
					<span class="where">{p.country}</span>
					{formatNumber(p.photographs)}
				</span>
				{#if p.first}
					<span class="when">{span(p)}</span>
				{/if}
			</button>
		{/each}
	</div>
{/if}

<style>
	.dim {
		color: var(--muted);
		font-size: var(--fs-m);
	}
	.sift {
		margin-bottom: 0.8rem;
		min-width: 14rem;
	}
	.wall {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 0.7rem;
	}
	.card {
		display: grid;
		gap: 0.15rem;
		padding: 0;
		border: 0;
		background: none;
		color: var(--text);
		cursor: pointer;
		text-align: left;
	}
	.card img,
	.blank {
		width: 100%;
		aspect-ratio: 4 / 3;
		object-fit: cover;
		border-radius: 7px;
		display: block;
		background: var(--surface-2);
	}
	.name {
		font-size: var(--fs-m);
		font-weight: 500;
		padding-top: 0.2rem;
	}
	.under {
		font-size: var(--fs-s);
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
	/* the country in the colour OPUS gives photographs, so a place reads as a
	   fact about pictures rather than as a heading */
	.where {
		color: var(--kind-photos);
		margin-right: 0.35rem;
	}
	.when {
		font-size: var(--fs-xs);
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
</style>

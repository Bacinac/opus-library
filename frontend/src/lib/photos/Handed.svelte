<script lang="ts">
	// Photographs somebody in the house chose for whoever holds this link. The
	// page knows the pictures and nothing about them: no date, no place, no
	// face, and no way further into the library.

	import { formatDate, plural, request } from '$lib/kit';
	import { Wordmark, groundOf, placeholderOf } from '$lib/opus';
	import PhotoViewer from '$lib/opus/PhotoViewer.svelte';
	import { t } from '$lib/i18n';
	import { MODULE } from '$lib/core/modules';

	let { key }: { key: string } = $props();

	type Photo = {
		id: string;
		kind: string;
		taken_at: null;
		dated: string;
		w: number | null;
		h: number | null;
		hash: string | null;
		ready: boolean;
		undecodable: boolean;
		turn: number;
	};

	let photos = $state<Photo[]>([]);
	let until = $state('');
	let gone = $state(false);
	let open = $state<number | null>(null);

	const base = $derived(`/api/shared/${encodeURIComponent(key)}`);
	const turned = (turn: number) => (turn ? `?turn=${turn}` : '');
	const tileAt = (id: string, turn: number) => `${base}/${id}/tile${turned(turn)}`;
	const previewAt = (id: string, turn: number) => `${base}/${id}/preview${turned(turn)}`;

	$effect(() => {
		request<{ expires_at: string; photos: Photo[] }>(base, {}, { on: { 404: () => (gone = true) } }).then(
			(said) => {
				if (!said) return;
				photos = said.photos;
				until = said.expires_at;
			}
		);
	});

	const shown = $derived(open === null ? null : photos[open]);
</script>

<svelte:head>
	<meta name="robots" content="noindex, nofollow" />
</svelte:head>

<main>
	<header>
		<Wordmark module={MODULE} />
		{#if photos.length}
			<p>
				<strong>{plural(photos.length, 'photos.count.one', 'photos.count.few', 'photos.count.many')}</strong>
				· {t('photos.shared.until', { date: formatDate(until) })}
			</p>
		{/if}
	</header>

	{#if gone}
		<p class="gone">{t('shared.gone')}</p>
	{:else}
		<div class="grid">
			{#each photos as photo, i (photo.id)}
				<button class="cell" type="button" onclick={() => (open = i)} aria-label={t('photos.photo')}>
					{#if photo.undecodable}
						<span class="broken">?</span>
					{:else if photo.ready}
						<img
							src={tileAt(photo.id, photo.turn)}
							alt=""
							loading="lazy"
							decoding="async"
							style:background-image={groundOf(photo.hash)}
						/>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
</main>

{#if shown && open !== null}
	<PhotoViewer
		photo={shown}
		placeholder={placeholderOf(shown.hash)}
		about={false}
		shows={previewAt}
		download={`${base}/${shown.id}/original`}
		hasPrev={open > 0}
		hasNext={open < photos.length - 1}
		onprev={() => open !== null && (open -= 1)}
		onnext={() => open !== null && (open += 1)}
		onclose={() => (open = null)}
		neighbours={[photos[open - 1], photos[open + 1]].filter((p) => p !== undefined)}
	/>
{/if}

<style>
	main {
		max-width: 80rem;
		margin: 0 auto;
		padding: 1rem;
	}
	header {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem 1rem;
		margin-bottom: 1rem;
	}
	header p {
		margin: 0;
		color: var(--muted);
		font-size: var(--fs-m);
	}
	header strong {
		color: var(--text);
	}
	.gone {
		margin: 4rem 0;
		text-align: center;
		color: var(--muted);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(9.75rem, 1fr));
		gap: 4px;
	}
	.cell {
		aspect-ratio: 1;
		overflow: hidden;
		padding: 0;
		border: 0;
		cursor: pointer;
		border-radius: 3px;
		background: var(--surface-2);
	}
	.cell img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
		background-size: cover;
		background-position: center;
	}
	.broken {
		display: grid;
		place-items: center;
		height: 100%;
		color: var(--muted);
		font-size: var(--fs-xl);
	}
</style>

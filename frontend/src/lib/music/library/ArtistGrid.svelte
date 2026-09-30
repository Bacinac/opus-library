<script lang="ts">
	// The library wall: one card per artist plus the letters beside it. The wall
	// is sorted by the alphabet the names file under, and the rail reads the
	// same alphabet, so a letter always has a card to go to.

	import { Letters, compareHr, initialOf, lettersOf } from '$lib/kit';
	import type { ArtistSummary } from '$lib/music/types';

	let { artists }: { artists: ArtistSummary[] } = $props();

	let sortedArtists = $derived([...artists].sort((a, b) => compareHr(a.name, b.name)));
	let letters = $derived(lettersOf(sortedArtists.map((a) => a.name)));

	function jump(letter: string) {
		const first = sortedArtists.find((a) => initialOf(a.name) === letter);
		if (!first) return;
		document
			.querySelector(`[data-artist="${first.id}"]`)
			?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}
</script>

<div class="grid">
	{#each sortedArtists as artist (artist.id)}
		<a class="card" href={`/artists/${artist.id}`} data-artist={artist.id}>
			{#if artist.image_url}
				<img src={artist.image_url} alt={artist.name} />
			{:else}
				<div class="placeholder"></div>
			{/if}
			<span>{artist.name}</span>
		</a>
	{/each}
</div>

<Letters {letters} onjump={jump} />

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: 1rem;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		text-decoration: none;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 0.75rem;
		scroll-margin-top: 5rem;
	}
	.card:hover {
		border-color: var(--accent);
	}
	.card img,
	.placeholder {
		width: 100%;
		aspect-ratio: 1;
		border-radius: 8px;
		object-fit: cover;
		background: var(--surface-2);
	}
	.card span {
		font-weight: 600;
	}
</style>

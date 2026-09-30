<script lang="ts">
	// Search, one catalogue at a time.
	//
	// A single box over both catalogues was the obvious idea and the wrong one:
	// music is searched artist-first (you type a name and want the discography),
	// film title-first, and one mixed ranking puts a film called Nick Cave next
	// to the man. The strip is the same strip; the catalogue behind it is not.

	import TypeStrip from '$lib/core/TypeStrip.svelte';
	import type { MediaType } from '$lib/core/types';
	import MusicSearch from '$lib/music/Search.svelte';
	import VideoSearch from '$lib/video/Search.svelte';

	let type = $state<MediaType | null>(null);
</script>

<TypeStrip bind:active={type} only={['music', 'movies', 'series']} />

{#if type === 'music'}
	<MusicSearch />
{:else if type === 'movies'}
	<VideoSearch kind="movies" />
{:else if type === 'series'}
	<VideoSearch kind="series" />
{/if}

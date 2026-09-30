<script lang="ts">
	// The library: everything this install holds, one media type at a time.
	//
	// The strip switches the whole vocabulary of the page rather than reskinning
	// one — artists have releases and tracks, films have subtitles, a series has
	// seasons, photographs have only a date — which is why each type mounts its
	// own component instead of a shared "item grid" that would have to be wrong
	// for four of the five.

	import { me } from '$lib/opus';
	import { t } from '$lib/i18n';
	import TypeStrip from '$lib/core/TypeStrip.svelte';
	import { install } from '$lib/core/install.svelte';
	import type { MediaType } from '$lib/core/types';
	import Elsewhere from '$lib/core/Elsewhere.svelte';
	import Artists from '$lib/music/Artists.svelte';
	import Photos from '$lib/photos/Photos.svelte';
	import LibraryScan from '$lib/video/LibraryScan.svelte';
	import Movies from '$lib/video/Movies.svelte';
	import Series from '$lib/video/Series.svelte';
	import WebVideo from '$lib/video/WebVideo.svelte';

	let type = $state<MediaType | null>(null);
</script>

{#if me.admin}
	<TypeStrip bind:active={type} />
{/if}

{#if me.guest}
	<!-- A guest is trusted with the address and not handed the family album, and
	     the album is the only reason anybody who does not maintain this place
	     comes here. So there is nothing on this page for them — said plainly and
	     with somewhere to go, rather than left as a shelf that answers "not
	     yours" to every request it makes. -->
	<Elsewhere />
{:else if !me.admin}
	<!-- a user came for the photographs: their own vault and the family's
	     shelf. The other four types are a library to be maintained, and
	     maintaining it is not theirs. -->
	<Photos />
{:else if install.loaded && install.types.length === 0}
	<p class="muted">{t('library.noTypes')}</p>
{:else if type === 'music'}
	<Artists />
{:else if type === 'movies'}
	<LibraryScan type="movies" />
	<Movies />
{:else if type === 'series'}
	<LibraryScan type="series" />
	<Series />
{:else if type === 'video'}
	<WebVideo />
{:else if type === 'photos'}
	<Photos />
{/if}

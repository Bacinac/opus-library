<script lang="ts">
	import { page } from '$app/state';
	import Handed from '$lib/photos/Handed.svelte';
</script>

<Handed key={page.params.key ?? ''} />

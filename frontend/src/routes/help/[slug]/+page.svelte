<script lang="ts">
	import { page } from '$app/state';
	import { HelpPage } from '#lib/kit/index.js';
	import { helpFor } from '#lib/opus/index.js';
</script>

<HelpPage help={helpFor('library')} slug={page.params.slug ?? ''} />

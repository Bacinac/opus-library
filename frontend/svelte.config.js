import adapterNode from '@sveltejs/adapter-node';
import adapterStatic from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { csp } from './src/lib/opus/csp.js';

const demo = process.env.OPUS_DEMO === '1';

export default {
	preprocess: vitePreprocess(),
	kit: {
		adapter: demo
			? adapterStatic({ fallback: 'index.html', precompress: true, strict: true })
			: adapterNode(),
		files: demo ? { appTemplate: 'src/app.demo.html' } : {},
		...(demo ? {} : { csp })
	}
};

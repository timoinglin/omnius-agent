import {useCurrentFrame, useVideoConfig} from 'remotion';

/**
 * Every beat of the piece, in SECONDS - never in frames. Keeping the beat map
 * in seconds is what lets Root.tsx switch between 30 and 60 fps without the
 * choreography drifting.
 *
 * The shape is a trailer: tension -> title hit -> punchline.
 */
export const BEAT = {
	/** god rays and dust become visible */
	raysIn: 0.6,
	/** the setup line fades up */
	leadIn: 1.3,
	/** ...and starts leaving */
	leadOut: 3.1,
	/** particles start being pulled toward the centre */
	charge: 2.5,
	/** everything darkens - the anticipation before the hit */
	compress: 3.72,
	/** THE HIT. Title, shockwave, screen shake, flash. */
	hit: 4.0,
	/** the punchline stamps in under the title */
	subtitle: 6.7,
	/** fade to black begins */
	fade: 9.0,
	/** ...and completes */
	black: 9.8,
} as const;

/** Current time in seconds. */
export const useSeconds = () => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	return frame / fps;
};

/** interpolate() without the surprise: never extrapolate past the ends. */
export const CLAMP = {
	extrapolateLeft: 'clamp',
	extrapolateRight: 'clamp',
} as const;

/** Seconds elapsed since a beat, floored at 0. */
export const since = (t: number, beat: number) => Math.max(0, t - beat);

/** A decaying oscillation - the shape of a physical impact settling down. */
export const decay = (elapsed: number, rate: number) => Math.exp(-elapsed * rate);

import React from 'react';
import {AbsoluteFill, Easing, interpolate} from 'remotion';
import {Backdrop, Compression, FadeOut, Vignette} from './Backdrop';
import {Dust, Embers} from './Particles';
import {Flash, Grain, Letterbox, LensFlare, Shockwave} from './Fx';
import {LeadLine, Subtitle, TitleBlock} from './Title';
import {BEAT, CLAMP, decay, since, useSeconds} from './timing';

/**
 * "PEDAZO DE MANCO" - a ten second trailer beat.
 *
 *   0.0 - 2.5s   tension: blacks, god rays, drifting dust, the setup line
 *   2.5 - 4.0s   charge: dust converges, the hairline brightens, frame darkens
 *   4.0s         THE HIT: flash, shockwave, screen shake, the title slams in
 *   4.0 - 6.7s   the metal settles, shimmer sweeps, embers rise
 *   6.7 - 9.0s   the punchline stamps in
 *   9.0 - 10.0s  fade to black
 *
 * Layer order matters: everything inside <Camera> is the scene and moves with
 * it. Grain, flash, vignette and the letterbox belong to the lens and the
 * print, so they sit outside and stay rock steady.
 */
export const PedazoDeManco: React.FC = () => {
	return (
		<AbsoluteFill style={{backgroundColor: '#000'}}>
			<Camera>
				<Backdrop />
				<Dust />
				<Shockwave />
				<LeadLine />
				<TitleBlock />
				<Subtitle />
				<Embers />
				<LensFlare />
			</Camera>

			<Compression />
			<Flash />
			<Vignette />
			<Grain />
			<Letterbox />
			<FadeOut />
		</AbsoluteFill>
	);
};

/**
 * Slow push-in for the whole piece, a pull-back-and-snap around the hit, and a
 * decaying shake on every impact.
 */
const Camera: React.FC<{children: React.ReactNode}> = ({children}) => {
	const t = useSeconds();

	// continuous push-in - a trailer never stops moving
	const push = interpolate(t, [0, BEAT.hit, 10], [1.03, 1.06, 1.14], CLAMP);

	// pull back a touch during the anticipation, then overshoot on the hit
	const squeeze = interpolate(t, [BEAT.compress, BEAT.hit], [0, -0.028], CLAMP);
	const pop = interpolate(t, [BEAT.hit, BEAT.hit + 0.6], [0.075, 0], {
		...CLAMP,
		easing: Easing.out(Easing.cubic),
	});

	// impact shake: title hit, then a smaller one when the punchline stamps
	const hitShake = t >= BEAT.hit ? 32 * decay(since(t, BEAT.hit), 6.5) : 0;
	const subShake = t >= BEAT.subtitle ? 10 * decay(since(t, BEAT.subtitle), 9) : 0;
	// nervous micro-tremor while the piece charges
	const micro = interpolate(t, [BEAT.charge, BEAT.hit], [0, 5], CLAMP);

	const amp = hitShake + subShake;
	const x = amp * Math.sin(t * 78) + micro * Math.sin(t * 47);
	const y = amp * 0.72 * Math.cos(t * 61) + micro * 0.6 * Math.cos(t * 39);
	const rot = amp * 0.024 * Math.sin(t * 53);

	return (
		<AbsoluteFill
			style={{
				transform: `scale(${push + squeeze + pop}) translate(${x}px, ${y}px) rotate(${rot}deg)`,
			}}
		>
			{children}
		</AbsoluteFill>
	);
};

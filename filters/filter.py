import os
os.environ["JAX_ENABLE_X64"] = "true"
import jax
import jax.numpy as jnp
from .resampling import resamplers

# Just a test of a very basic filter
class ParticleFilter:

    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, resampling: str = "default", observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps # no of steps of numerical model in between DA steps
        self.n_dim = n_dim # dimension of the state space (usually no of discretized grid points)
        self.fwd_model = forward_model # forward model for the ensemble
        self.signal_model = signal_model # forward model for the signal
        self.sigma = sigma # observation error standard deviation
        self.resample = resamplers[resampling]
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)

    def advance_signal(self, signal_position, key):
        signal, _ = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        prediction, _ = self.fwd_model.run(particles, self.n_steps, None, key)
        return prediction

    def observation_from_signal(self, signal, key):
        key, subkey = jax.random.split(key)
        observed = signal + self.sigma * jax.random.normal(subkey, shape=signal.shape)
        # observed = signal + self.sigma * jax.random.normal(key, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        particles_observed = jnp.zeros_like(particles)
        particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
        # since we are always resampling, we do not need to sequentially update the weights
        log_weights = v_get_log_weight(particles_observed, observation, self.sigma)
        particles = self.resample(particles, jax.nn.softmax(log_weights), key)
        return particles

    def run_step(self, particles, signal, key):
        key, obs_key, sampling_key, pred_key, sig_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal, sig_key)
        particles = self.predict(particles, pred_key)
        observation = self.observation_from_signal(signal, obs_key)

        particles = self.update(particles, observation, sampling_key)
        return particles, signal, observation

    def run(self, initial_particles, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        def scan_fn(val, i):
            particles, signal, key = val
            key, next_key = jax.random.split(key)
            particles, signal, observation = self.run_step(particles, signal, key)
            return (particles, signal, next_key), (particles, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles, initial_signal, key), jnp.arange(n_total))
        return final, all
    

class ParticleFilter_Sequential:
    """ this differs from the above in that resampling is done conditionally on the ess, based on sequential likelihood of the weights."""

    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, resampling: str = "default", observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps # no of steps of numerical model in between DA steps
        self.n_dim = n_dim # dimension of the state space (usually no of discretized grid points)
        self.fwd_model = forward_model # forward model for the ensemble
        self.signal_model = signal_model # forward model for the signal
        self.sigma = sigma # observation error standard deviation
        self.resample = resamplers[resampling]
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)
        self.weights = jnp.ones((n_particles,)) / n_particles  # Initialize weights uniformly
    
    def advance_signal(self, signal_position, key):
        signal, _ = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        prediction, _ = self.fwd_model.run(particles, self.n_steps, None, key)
        return prediction

    def observation_from_signal(self, signal, key):
        key, subkey = jax.random.split(key)
        observed = signal + self.sigma * jax.random.normal(subkey, shape=signal.shape)
        # observed = signal + self.sigma * jax.random.normal(key, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, weights, particles, observation, key):
        " this one differs"
        
        particles_observed = jnp.zeros_like(particles)
        particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
        log_weights = v_get_log_weight(particles_observed, observation, self.sigma)

        likelyhood = jnp.exp(log_weights)
        weights = weights * likelyhood
        ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(jnp.exp(weights)))

        particles = jax.lax.cond(
                    ess < 1/2,
                    lambda particles: self.resample(particles, jax.nn.softmax(log_weights), key),
                    lambda particles: particles,
                    particles
                )

        return particles, weights

    def run_step(self, weights, particles, signal, key):
        key, obs_key, sampling_key, pred_key, sig_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal, sig_key)
        particles = self.predict(particles, pred_key)
        observation = self.observation_from_signal(signal, obs_key)

        particles,weights = self.update(particles, weights, observation, sampling_key)
        return particles, weights, signal, observation

    def run(self, initial_particles, initial_weights, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        def scan_fn(val, i):
            particles, weights, signal, key = val
            key, next_key = jax.random.split(key)
            particles, weights, signal, observation = self.run_step(particles, signal, key)
            return (particles, weights, signal, next_key), (particles, weights, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles,initial_weights, initial_signal, key), jnp.arange(n_total))
        return final, all


# Particle filter with basic tempering and jittering
class ParticleFilterTJ:

    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, resampling: str = "default", observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps # no of steps of numerical model in between DA steps
        self.n_dim = n_dim # dimension of the state space (usually no of discretized grid points)
        self.fwd_model = forward_model # forward model for the ensemble
        self.signal_model = signal_model # forward model for the signal
        self.sigma = sigma # observation error standard deviation
        self.resample = resamplers[resampling]
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)
        self.max_iter_jitter = 10
        
    def advance_signal(self, signal_position):
        signal, _ = self.signal_model.run(signal_position, self.n_steps, None)
        return signal

    def predict(self, particles):
        prediction, _ = self.fwd_model.run(particles, self.n_steps, None)
        return prediction

    def observation_from_signal(self, signal, key):
        key, subkey = jax.random.split(key)
        observed = signal + self.sigma * jax.random.normal(subkey, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        particles_observed = jnp.zeros_like(particles)
        particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
        log_weights = v_get_log_weight(particles_observed, observation, self.sigma)
        pos_out, _, log_weights = self.tj(particles_observed, log_weights, observation, self.n_particles)
        particles = self.resample(pos_out[-1], jax.nn.softmax(log_weights), key)
        return particles
    

    def tj(self, positions_0, log_weights_0, observation, nens):
        jitter_param = 1e-3

        pos_out = []
        pos_out.append(positions_0)

        key = jax.random.PRNGKey(10)
        ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(log_weights_0))
        rel_ess_target = 0.8
        phi = 0.
        phi_remaining = 1.
        iterations = 0
        parents = jnp.arange(nens, dtype=jnp.int32)
        origin = jnp.arange(nens, dtype=jnp.int32)
        log_weights = log_weights_0
        positions = positions_0
        while ess < rel_ess_target and iterations < self.max_iter_jitter:
            branching_key, key = jax.random.split(key)
            phi_next = find_phi_temp(log_weights, rel_ess_target, phi)
            dphi = phi_next - phi
            temp_norm_weights = jax.nn.softmax(dphi*log_weights)
            parents = branching(temp_norm_weights, branching_key)
            positions = positions[parents]
            origin = origin[parents]
            log_weights = v_get_log_weight(positions[:,None], observation, 0.1)

            duplicates = find_duplicates(parents)# shape (44,)

            jitter_key, key = jax.random.split(key)
            jitter = jitter_param*jax.random.uniform(jitter_key, (duplicates.shape[0],self.n_dim), minval=-1., maxval=1.)

            proposed_positions = positions[duplicates]+jitter
            proposed_weights = v_get_log_weight(proposed_positions[:,None], observation, 0.1)
            likelihood_ratio = jnp.exp(phi*(proposed_weights - log_weights[duplicates]))
            prob = likelihood_ratio.clip(max=1.)
            accept_key, key = jax.random.split(key)
            accept = jax.random.bernoulli(accept_key, prob)
            # (44,),
            positions_update = jnp.where(accept[:,jnp.newaxis], proposed_positions, positions[duplicates])
            positions = positions.at[duplicates].set(positions_update)

            log_weights = v_get_log_weight(positions[:,None], observation, 0.1)

            pos_out.append(positions)
            phi = phi_next
            phi_remaining = 1. - phi

            ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(phi_remaining*log_weights))
            #ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(phi*log_weights))

            iterations += 1
            print(f'ess:{ess}, iter{iterations}')

            if ess > rel_ess_target:
                branching_key, key = jax.random.split(key)
                temp_norm_weights = jax.nn.softmax(phi_remaining*log_weights)
                parents = branching(temp_norm_weights, branching_key)
                positions = positions[parents]
                origin = origin[parents]
                log_weights = v_get_log_weight(positions[:,None], observation, 0.1)
                duplicates = find_duplicates(parents)
                jitter_key, key = jax.random.split(key)
                jitter = jitter_param * jax.random.uniform(jitter_key, (duplicates.shape[0],self.n_dim), minval=-1., maxval=1.)
                proposed_positions = positions[duplicates]+jitter
                proposed_weights = v_get_log_weight(proposed_positions[:,None], observation, 0.1)
                likelihood_ratio = jnp.exp(proposed_weights - log_weights[duplicates])
                prob = likelihood_ratio.clip(max=1.)
                accept_key, key = jax.random.split(key)
                accept = jax.random.bernoulli(accept_key, prob)
                positions_update = jnp.where(accept[:,jnp.newaxis], proposed_positions, positions[duplicates])
                positions = positions.at[duplicates].set(positions_update)

                pos_out.append(positions)
        
        return pos_out, origin, log_weights

    def run_step(self, particles, signal, key):
        key, obs_key, sampling_key, sig_key, pred_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal,sig_key)
        particles = self.predict(particles,pred_key)
        observation = self.observation_from_signal(signal, obs_key)

        particles = self.update(particles, observation, sampling_key)
        return particles, signal, observation

    def run(self, initial_particles, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        all_particles = []
        all_signals = []
        all_observations = []

        particles = initial_particles
        signal = initial_signal

        for _ in range(n_total):
            key, next_key = jax.random.split(key)
            particles, signal, observation = self.run_step(particles, signal, key)
            all_particles.append(particles)
            all_signals.append(signal)
            all_observations.append(observation)

        final = (particles, signal)
        all = (jnp.array(all_particles), jnp.array(all_signals), jnp.array(all_observations))
        return final, all

# this class also outputs the solution when data is not available. 
class ParticleFilterAll:

    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, resampling: str = "default", observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps # no of steps of numerical model in between DA steps
        self.n_dim = n_dim # dimension of the state space (usually no of discretized grid points)
        self.fwd_model = forward_model # forward model for the ensemble
        self.signal_model = signal_model # forward model for the signal
        self.sigma = sigma # observation error standard deviation
        self.resample = resamplers[resampling]
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)

    def advance_signal(self, signal_position,key):
        _, signal = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        _, prediction = self.fwd_model.run(particles, self.n_steps, None, key)
        return prediction

    def observation_from_signal(self, signal, key):
        observed = signal + self.sigma * jax.random.normal(key, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        particles_observed = jnp.zeros_like(particles)
        particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
        log_weights = v_get_log_weight(particles_observed, observation, self.sigma)
        particles = self.resample(particles, jax.nn.softmax(log_weights), key)
        return particles

    def run_step(self, particles, signal, key):
        key, obs_key, sampling_key, sig_key, pred_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal, sig_key)
        particles = self.predict(particles, pred_key)
        observation = self.observation_from_signal(signal[-1,:,:], obs_key)

        updated_particles = self.update(particles[-1,:,:], observation, sampling_key)
        particles = particles.at[-1,:,:].set(updated_particles)
        return particles, signal, observation

    def run(self, initial_particles, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        def scan_fn(val, i):
            particles, signal, key = val
            key, next_key = jax.random.split(key)
            particles, signal, observation = self.run_step(particles[-1,:,:], signal[-1,:,:], key)
            return (particles[-1,:,:][None,...], signal[-1,:,:][None,...], next_key), (particles, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles, initial_signal, key), jnp.arange(n_total))
        return final, all
    



class ParticleFilter_tempered_jittered:

    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, resampling: str = "default", observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps # no of steps of numerical model in between DA steps
        self.n_dim = n_dim # dimension of the state space (usually no of discretized grid points)
        self.fwd_model = forward_model # forward model for the ensemble
        self.signal_model = signal_model # forward model for the signal
        self.sigma = sigma # observation error standard deviation
        self.resample = resamplers[resampling]
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)
        self.weights = jnp.ones((n_particles,)) / n_particles  # Initialize weights uniformly 
    
    def advance_signal(self, signal_position, key):
        signal, _ = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        prediction, _ = self.fwd_model.run(particles, self.n_steps, None, key)
        return prediction

    def observation_from_signal(self, signal, key):
        key, subkey = jax.random.split(key)
        observed = signal + self.sigma * jax.random.normal(subkey, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        particles_observed = jnp.zeros_like(particles)
        particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
        log_weights = v_get_log_weight(particles_observed, observation, self.sigma)
        # likelyhood = jnp.exp(log_weights)
        # weights = weights * likelyhood
        # ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(jnp.exp(weights)))

        num_tempering_steps = 1
        beta_schedule = jnp.linspace(0, 1, num_tempering_steps)

        # def resampling_and_jittering(particles, log_weights, key):
        #         particles = self.resample(particles, jax.nn.softmax(log_weights), key)
        #         jitter_key, key = jax.random.split(key)
        #         jitter = 0.0001 * jax.random.uniform(jitter_key, particles.shape, minval=-1., maxval=1.)
        #         return particles + jitter
        
        # def resampling_and_jittering_no_jitter(particles, log_weights, key):
        #         return self.resample(particles, jax.nn.softmax(log_weights), key)

        for beta_idx in range(len(beta_schedule) - 1):
            beta_current = beta_schedule[beta_idx]
            beta_next = beta_schedule[beta_idx + 1]
            delta_beta = beta_next - beta_current

            particles_observed = jnp.zeros_like(particles)
            particles_observed = particles_observed.at[..., self.observation_locations].set(particles[..., self.observation_locations])
            log_weights = v_get_log_weight(particles_observed*delta_beta, observation*delta_beta, self.sigma)
            ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(log_weights))
            
            particles = jax.lax.cond(
                    ess < 1/2,
                    lambda particles: self.resample(particles, jax.nn.softmax(log_weights), key),
                    lambda particles: particles,
                    particles
                )
            
            jitter_key, key = jax.random.split(key)
            jitter = 0.001 * jax.random.uniform(jitter_key, particles.shape, minval=-1., maxval=1.)
            particles = particles + jitter
            

        return particles

    def run_step(self, particles, signal, key):
        key, obs_key, sampling_key, sig_key, pred_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal,sig_key)
        particles = self.predict(particles, pred_key)
        observation = self.observation_from_signal(signal, obs_key)

        particles = self.update(particles, observation, sampling_key)
        return particles, signal, observation

    def run(self, initial_particles, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        def scan_fn(val, i):
            particles, signal, key = val
            key, next_key = jax.random.split(key)
            particles, signal, observation = self.run_step(particles, signal, key)
            return (particles, signal, next_key), (particles, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles, initial_signal, key), jnp.arange(n_total))
        return final, all

class EnsembleKalmanFilter:
    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps
        self.n_dim = n_dim
        self.fwd_model = forward_model
        self.signal_model = signal_model
        self.sigma = sigma
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)

    def advance_signal(self, signal_position, key):
        signal, _ = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        prediction, _ = self.fwd_model.run(particles, self.n_steps, None, key)# final,all.
        return prediction

    def observation_from_signal(self, signal, key):
        key, subkey = jax.random.split(key)
        observed = signal + self.sigma * jax.random.normal(subkey, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        # Observation space lambda function, 
        H = lambda x: x[..., self.observation_locations]
        # Ensemble mean over the first axis.
        assert particles.shape[0] == self.n_particles, "Number of particles does not match n_particles"
        mean = jnp.mean(particles, axis=0)
        X = particles - mean  # anomalies
        Y = jax.vmap(H)(particles)# Apply observation operator to each particle
        #print(f"Y shape: {Y.shape}, particles shape: {particles.shape}")
        # e.g. Y shape: (128, 16), particles shape: (128, 256)
        y_mean = jnp.mean(Y, axis=0)
        Y_perturb = Y - y_mean
        # shape 128,16

        # Observation noise perturbation
        key, subkey = jax.random.split(key)
        obs_perturb = self.sigma * jax.random.normal(subkey, shape=Y.shape)
        y_obs = H(observation) + obs_perturb
        # perturbed observation shape: (128, 16)

        # Compute covariances
        # (128, 256).T @ (128, 16) -> (256, 16)
        Pf_HT = X.T @ Y_perturb / (self.n_particles - 1)
        # (128, 16).T @ (128, 16) -> (16, 16)
        S = Y_perturb.T @ Y_perturb / (self.n_particles - 1) + self.sigma**2 * jnp.eye(Y.shape[1])
        # Kalman gain on lower dimensional observation subspace. 
        K = Pf_HT @ jnp.linalg.inv(S) # P H^{T} (HPH^T - R)^{-1} # R is the observation noise covariance.
        # (256, 16) @ (16, 16) -> (256, 16)
        #K = jax.scipy.linalg.solve(S, Pf_HT.T).T # Kalman gain using solve instead of inverse, if wanting to use jax scipy.
        # Update
        innovations = y_obs - Y # y_{obs}-Hx shape (128, 16)
        update = jax.vmap(lambda innov: K @ innov)(innovations)  # shape: (n_particles, n_dim)
        particles = particles + update 

        return particles

    def run_step(self, particles, signal, key):
        key, obs_key, update_key, pred_key, sig_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal, sig_key)
        particles = self.predict(particles, pred_key)
        observation = self.observation_from_signal(signal, obs_key)
        particles = self.update(particles, observation, update_key)
        return particles, signal, observation

    def run(self, initial_particles, initial_signal, n_total, key):
        def scan_fn(val, i):
            particles, signal, key = val
            key, next_key = jax.random.split(key)
            particles, signal, observation = self.run_step(particles, signal, key)
            return (particles, signal, next_key), (particles, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles, initial_signal, key), jnp.arange(n_total))
        return final, all
    


# the point is to do full solution output. 
# this class also outputs the solution when data is not available
class EnsembleKalmanFilter_All:
    def __init__(self, n_particles, n_steps, n_dim, forward_model, signal_model, sigma, observation_locations=None):
        self.n_particles = n_particles
        self.n_steps = n_steps
        self.n_dim = n_dim
        self.fwd_model = forward_model
        self.signal_model = signal_model
        self.sigma = sigma
        self.observation_locations = slice(observation_locations) if observation_locations is None else tuple(observation_locations)

    def advance_signal(self, signal_position, key):
        _, signal = self.signal_model.run(signal_position, self.n_steps, None, key)
        return signal

    def predict(self, particles, key):
        _, prediction = self.fwd_model.run(particles, self.n_steps, None, key)
        # final, all, implies prediction is a array of shape (n_steps(between da), n_particles, n_dim)
        return prediction 
    
    # def one_step_predict(self, particles):
    #     """Predicts the next state of the particles using the forward model."""
    #     prediction, _ = self.fwd_model.run(particles, 1, None)
    #     return prediction

    def observation_from_signal(self, signal, key):
        observed = signal + self.sigma * jax.random.normal(key, shape=signal.shape)
        observation = jnp.zeros_like(signal)
        observation = observation.at[..., self.observation_locations].set(observed[..., self.observation_locations])
        return observation

    def update(self, particles, observation, key):
        H = lambda x: x[..., self.observation_locations]
        mean = jnp.mean(particles, axis=0)
        X = particles - mean  
        Y = jax.vmap(H)(particles)
        y_mean = jnp.mean(Y, axis=0)
        Y_perturb = Y - y_mean
        key, subkey = jax.random.split(key)
        obs_perturb = self.sigma * jax.random.normal(subkey, shape=Y.shape)
        y_obs = H(observation) + obs_perturb
        Pf_HT = X.T @ Y_perturb / (self.n_particles - 1)
        S = Y_perturb.T @ Y_perturb / (self.n_particles - 1) + self.sigma**2 * jnp.eye(Y.shape[1])
        K = Pf_HT @ jnp.linalg.inv(S) 
        innovations = y_obs - Y
        update = jax.vmap(lambda innov: K @ innov)(innovations)
        particles = particles + update 
        return particles
    
    def run_step(self, particles, signal, key):
        key, obs_key, sampling_key, pred_key, sig_key = jax.random.split(key, 5)
        signal = self.advance_signal(signal, sig_key)# self.nstep update
        particles = self.predict(particles, pred_key)# all time output. 
        observation = self.observation_from_signal(signal[-1,:,:], obs_key)
        updated_particles = self.update(particles[-1,:,:], observation, sampling_key)
        particles = particles.at[-1,:,:].set(updated_particles)
        return particles, signal, observation #particles] 

    def run(self, initial_particles, initial_signal, n_total, key):
        """_summary: Runs the initial particles_

        Args:
            initial_particles (_type_): _description_
            initial_signal (_type_): _description_
            n_total (_type_): _n_total is the number of data assimilation proceedures._
        """
        def scan_fn(val, i):
            particles, signal, key = val # get the carry, 
            key, next_key = jax.random.split(key)
            particles_old = particles[-1,:,:]# initially we do not have this dimension.
            signal_old = signal[-1,:,:]
            particles, signal, observation = self.run_step(particles_old, signal_old, key) # 
            particles_new = particles[-1,:,:][None,...]
            signal_new = signal[-1,:,:][None,...]
            return (particles_new, signal_new, next_key), (particles, signal, observation)
        
        final, all = jax.lax.scan(scan_fn, (initial_particles[None,...], initial_signal[None,...], key), jnp.arange(n_total))
        #final = jnp.concatenate(final, axis=0)#final.reshape(-1, self.n_particles, self.n_dim)  # reshape to (n_steps, n_particles, n_dim)
        #all = jnp.concatenate(all, axis=0)#all.reshape(-1, self.n_particles, self.n_dim)  # reshape to (n_steps, n_particles, n_dim)
        return final, all
    
    

    



# # Some helper functions that may be useful for the various filters (not all tested, and some may be redundant)

def find_duplicates(arr):
    unique_elements, _, counts = jnp.unique(arr, return_index=True, return_counts=True)
    duplicates_mask = counts > 1
    duplicate_values = unique_elements[duplicates_mask]

    duplicate_indices = []
    for value in duplicate_values:
        duplicate_indices.append(jnp.where(arr == value)[0])

    if duplicate_indices:
        duplicate_indices = jnp.concatenate(duplicate_indices, dtype=jnp.int32)
    else:
        duplicate_indices = jnp.asarray([], dtype=jnp.int32)

    return duplicate_indices

def get_log_weight(particle, observation, sigma):
    """Returns the likelihood weight of a particle for a given observation.
    # TODO: consider .reshape(-1), as to not create a copy,
    """
    return -0.5*jnp.sum((particle.flatten()-observation.flatten())**2)/sigma**2

v_get_log_weight = jax.jit(jax.vmap(get_log_weight, in_axes=(0, None, None)))


@jax.jit
def branching(weights, key):
    """Perform a branching step"""

    #TODO Change to eliminate the positions array
    positions = jnp.arange(len(weights), dtype=jnp.int32)
    offspring = compute_offspring(weights, key)
    parent = reindex(positions, offspring)

    return parent

@jax.jit
def get_normalized_weights(particles, observation, sigma):
    """Returns the normalized weights of the particles for a given observation.
    """
    log_weights = v_get_log_weight(particles, observation, sigma)
    return jax.nn.softmax(log_weights)

@jax.jit
def get_rel_ess(log_weights):
    """Returns the relative effective sample size of the weights.
    """
    weights = jax.nn.softmax(log_weights)
    return 1./jnp.sum(weights**2)/weights.shape[0]

@jax.jit
def get_rel_ess_from_normalized_weights(weights):
    """Returns the relative effective sample size of the weights.
    """
    return 1./jnp.sum(weights**2)/weights.shape[0]

def pick_sample(fields, sample_idxs):
    """Picks a sample from the particle filter.
    """
    u,v,e = fields
    return u[sample_idxs],v[sample_idxs],e[sample_idxs]

@jax.jit
def branching_log(log_weights, key):
    """Perform a branching step"""
    weights = jax.nn.softmax(log_weights)
    #TODO Change to eliminate the positions array
    positions = jnp.arange(len(weights), dtype=jnp.int32)
    offspring = compute_offspring(weights, key)
    parent = reindex(positions, offspring)

    return parent


@jax.jit
def compute_offspring(weights, key):
    """Compute the number of offspring for each particle"""

    def inner_cond_true(unif, offspr, j, n, weights, h, g):
        offspr = jax.lax.cond(unif[j] < 1. - (((n*weights[j]) % 1)/((g % 1)+1e-10)),
                    lambda offspr, j, n, weights, h, g: offspr.at[j].set(jnp.floor(n*weights[j])),
                    lambda offspr, j, n, weights, h, g: offspr.at[j].set(jnp.floor(n*weights[j]) + h - jnp.floor(g)), # pylint: disable=line-too-long
                    offspr, j, n, weights, h, g  )
        return offspr

    def inner_cond_false(unif, offspr, j, n, weights, h, g):
        offspr = jax.lax.cond(unif[j] < 1. - (1. - ((n*weights[j]) % 1) ) / (1 - (g % 1)+1e-10),
                    lambda offspr, j, n, weights, h, g: offspr.at[j].set(jnp.floor(n*weights[j]) + 1.), # pylint: disable=line-too-long
                    lambda offspr, j, n, weights, h, g: offspr.at[j].set(jnp.floor(n*weights[j]) + h - jnp.floor(g)), # pylint: disable=line-too-long
                    offspr, j, n, weights, h, g  )
        return offspr

    def outer_cond(unif, offspr, j, n, weights, h, g):
        offspr = jax.lax.cond(((n*weights[j]) % 1) + ((g - n*weights[j]) % 1) < 1.,
                    inner_cond_true,
                    inner_cond_false,
                    unif, offspr, j, n, weights, h, g  )
        return offspr

    def body_fun(i, val):
        unif, offspr, n, weights, h, g = val
        offspr = outer_cond(unif, offspr, i, n, weights, h, g)
        g = g - n*weights[i]
        h = h - offspr[i]
        return unif, offspr, n, weights, h, g

    n = weights.shape[0]
    unif = jax.random.uniform(key, shape=(n-1,))
    g = n
    h = n
    offspr = jnp.empty_like(weights)

    unif, offspr, n, weights, h, g = jax.lax.fori_loop(0, n-1, body_fun, (unif, offspr, n, weights, h, g)) # pylint: disable=line-too-long

    offspr = offspr.at[n-1].set(h)
    offspr = offspr.astype(int)
    return offspr

@jax.jit
def reindex(positions, offspring):
    """Reindex the particles according to their offspring"""

    # TODO Change to eliminate the positions array

    def body_fun_inner(i, val):
        pos, parent, r, positions, j = val
        pos = pos.at[r+i].set(positions[j])
        parent = parent.at[r+i].set(j) # parent of particle r+i is particle j
        return pos, parent, r, positions, j

    def body_fun_outer(j, val):
        pos, parent, r, positions, offspring = val
        pos, parent, r, positions, _ = jax.lax.fori_loop(0, offspring[j], body_fun_inner, (pos, parent, r, positions, j)) # pylint: disable=line-too-long
        r = r + offspring[j]
        return pos, parent, r, positions, offspring

    n = len(positions)
    pos = jnp.empty_like(positions)
    parent = jnp.empty_like(offspring)
    r = 0
    pos, parent, r, positions, offspring = jax.lax.fori_loop(0, n, body_fun_outer, (pos, parent, r, positions, offspring)) # pylint: disable=line-too-long

    return parent



def find_phi_temp(log_wgts, rel_ess_target, prev_phi, max_iter = 20):
    N_ens = len(log_wgts)
    phi = 0.5 * (1. + prev_phi)
    for _iter in range(max_iter):
        ess = 1./jnp.linalg.norm(temper_weights(log_wgts, phi-prev_phi))**2
        if ess >= rel_ess_target * N_ens:
            #print(f'Target ESS reached after {_iter+1} iterations. ESS = {ess}')
            return phi
        phi = 0.5*(phi + prev_phi)
    #print(f'Target ESS not reached within {max_iter} iterations. ESS = {ess}')
    return phi

def temper_weights(log_wgts, temp):
    norm_temp_wgts = jnp.exp(temp*log_wgts - jax.nn.logsumexp(temp*log_wgts))
    return norm_temp_wgts

def log_temper_weights(log_wgts, temp):
    return jax.nn.log_softmax(temp*log_wgts)

def tempering_step(key, phi, log_lkhd, positions, rel_ess_target):
    phi_next = find_phi_temp(log_lkhd, rel_ess_target, phi)
    dphi = phi_next - phi
    temp_norm_weights = jax.nn.softmax(dphi*log_lkhd)
    parents = branching(temp_norm_weights, key)
    new_positions = positions[parents]

    return phi_next, new_positions, parents

def tempering(key, positions, weights, observation, rel_ess_target):

    phi = 0.
    phi_remaining = 1.
    iterations = 0
    nens = len(weights)
    origin = jnp.arange(nens, dtype=jnp.int32)
    log_weights = jnp.log(weights)
    ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(phi_remaining*log_weights))
    temp_positions = positions

    while ess < rel_ess_target:
        tempering_key, key = jax.random.split(key)

        phi, temp_positions, parents = tempering_step(tempering_key, phi, log_weights, temp_positions, rel_ess_target)

        origin = origin[parents]

        phi_remaining = 1. - phi
        log_weights = v_get_log_weight(positions, observation, 0.1)
        ess = get_rel_ess_from_normalized_weights(jax.nn.softmax(phi_remaining*log_weights))
        iterations += 1





    
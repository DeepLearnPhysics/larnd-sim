import os, sys
larndsim_dir=os.path.abspath(os.path.join(os.path.dirname( __file__ ), '..'))
sys.path.insert(0, larndsim_dir)
import shutil
import pickle
import numpy as np
from .utils import get_id_map, all_sim, embed_adc_list, calc_loss
from .ranges import ranges
from larndsim.sim_with_grad import sim_with_grad
import torch

from tqdm import tqdm

class ParamFitter:
    def __init__(self, relevant_params, track_fields, track_chunk, pixel_chunk,
                 detector_props, pixel_layouts, load_checkpoint = None,
                 lr=None, optimizer=None, loss_fn=None, readout_noise=True):

        # If you have access to a GPU, sim works trivially/is much faster
        if torch.cuda.is_available():
            self.device = 'cuda'
            # torch.set_default_tensor_type('torch.cuda.FloatTensor')
        else:
            self.device = 'cpu'
        self.track_fields = track_fields
        if type(relevant_params) == dict:
            self.relevant_params_list = list(relevant_params.keys())
            self.relevant_params_dict = relevant_params
        elif type(relevant_params) == list:
            self.relevant_params_list = relevant_params
            self.relevant_params_dict = None
        else:
            raise TypeError("relevant_params must be list of param names or dict with learning rates")

        is_continue = False
        if load_checkpoint is not None:
            history = pickle.load(open(load_checkpoint, "rb"))
            is_continue = True

        # Simulation object for target
        self.sim_target = sim_with_grad(track_chunk=track_chunk, pixel_chunk=pixel_chunk, readout_noise=readout_noise)
        self.sim_target.load_detector_properties(detector_props, pixel_layouts)

        # Simulation object for iteration -- this is where gradient updates will happen
        self.sim_iter = sim_with_grad(track_chunk=track_chunk, pixel_chunk=pixel_chunk, readout_noise=readout_noise)
        self.sim_iter.load_detector_properties(detector_props, pixel_layouts)

        # Normalize parameters to init at 1, or set to checkpointed values
        for param in self.relevant_params_list:
            if is_continue:
                setattr(self.sim_iter, param, history[param][-1])
            else:
                setattr(self.sim_iter, param, getattr(self.sim_iter, param)/ranges[param]['nom'])

        # Keep track of gradients in sim_iter
        self.sim_iter.track_gradients(self.relevant_params_list)

        # Placeholder simulation -- parameters will be set by un-normalizing sim_iter
        self.sim_physics = sim_with_grad(track_chunk=track_chunk, pixel_chunk=pixel_chunk, readout_noise=readout_noise)
        self.sim_physics.load_detector_properties(detector_props, pixel_layouts)

        # Set up optimizer -- can pass in directly, or construct as SGD from relevant params and/or lr
        if optimizer is None:
            if self.relevant_params_dict is None:
                if lr is None:
                    raise ValueError("Need to specify lr for params")
                else:
                    self.optimizer = torch.optim.SGD([getattr(self.sim_iter, param) for param in self.relevant_params_list], lr=lr)
            else:
                 self.optimizer = torch.optim.SGD(self.relevant_params_dict)

        else:
            self.optimizer = optimizer

        # Set up loss function -- can pass in directly, or sparse diff by default
        if loss_fn is None:
            self.loss_fn = calc_loss
        else:
            self.loss_fn = loss_fn

        if is_continue:
            self.training_history = history
        else:
            self.training_history = {}
            for param in self.relevant_params_list:
                self.training_history[param] = []
            self.training_history['losses'] = []


    def make_target_sim(self, seed=2):
        np.random.seed(seed)
        print("Constructing target param simulation")
        for param in self.relevant_params_list:
            param_val = np.random.uniform(low=ranges[param]['down'], 
                                          high=ranges[param]['up'])

            print(f'{param}, target: {param_val}, init {getattr(self.sim_target, param)}')    
            setattr(self.sim_target, param, param_val)

    def fit(self, dataloader, epochs=300, shuffle=False, save_freq=5, print_freq=1):
        # make a folder for the pixel target
        if os.path.exists('target'):
            shutil.rmtree('target', ignore_errors=True)
        os.makedirs('target')


        # Include initial value in training history (if haven't loaded a checkpoint)
        for param in self.relevant_params_list:
            if len(self.training_history[param]) == 0:
                self.training_history[param].append(getattr(self.sim_iter, param).item())

        # The training loop
        with tqdm(total=len(dataloader) * epochs) as pbar:
            for epoch in range(epochs):

                # Losses for each batch -- used to compute epoch loss
                losses_batch=[]
                for i, selected_tracks_bt_torch in enumerate(dataloader):
                    # Zero gradients
                    self.optimizer.zero_grad()

                    # Get rid of the extra dimension and padding elements for the loaded data
                    selected_tracks_bt_torch = torch.flatten(selected_tracks_bt_torch, start_dim=0, end_dim=1)
                    selected_tracks_bt_torch = selected_tracks_bt_torch[selected_tracks_bt_torch[:, self.track_fields.index("dx")] > 0]
                    event_id_map, unique_eventIDs = get_id_map(selected_tracks_bt_torch, self.track_fields, self.device)

                    loss_ev = []
                    # Calculate loss per event
                    for ev in unique_eventIDs:
                        selected_tracks_torch = selected_tracks_bt_torch[selected_tracks_bt_torch[:, self.track_fields.index("eventID")] == ev]
                        selected_tracks_torch = selected_tracks_torch.to(self.device)

                        if shuffle:
                            target, pix_target, ticks_list_targ = all_sim(self.sim_target, selected_tracks_torch, self.track_fields,
                                                                          event_id_map, unique_eventIDs,
                                                                          return_unique_pix=True)
                            embed_target = embed_adc_list(self.sim_target, target, pix_target, ticks_list_targ)
                        else:
                            # Simulate target and store them
                            if epoch == 0:
                                
                                target, pix_target, ticks_list_targ = all_sim(self.sim_target, selected_tracks_torch, self.track_fields,
                                                                              event_id_map, unique_eventIDs,
                                                                              return_unique_pix=True)
                                embed_target = embed_adc_list(self.sim_target, target, pix_target, ticks_list_targ)

                                torch.save(embed_target, 'target/batch' + str(i) + '_ev' + str(int(ev))+ '_target.pt')

                            else:
                                embed_target = torch.load('target/batch' + str(i) + '_ev' + str(int(ev))+ '_target.pt')

                        # Undo normalization (sim -> sim_physics)
                        for param in self.relevant_params_list:
                            setattr(self.sim_physics, param, getattr(self.sim_iter, param)*ranges[param]['nom'])
                            print(param, getattr(self.sim_physics, param))

                        # Simulate and get output
                        output, pix_out, ticks_list_out = all_sim(self.sim_physics, selected_tracks_torch, self.track_fields,
                                                  event_id_map, unique_eventIDs,
                                                  return_unique_pix=True)

                        # Embed both output and target into "full" image space
                        embed_output = embed_adc_list(self.sim_physics, output, pix_out, ticks_list_out)

                        # Calc loss between simulated and target + backprop
                        loss = self.loss_fn(self.sim_physics, embed_output, embed_target)

                        # To be investigated -- sometimes we get nans. Avoid doing a step if so
                        if not loss.isnan():
                            loss_ev.append(loss)

                    # Backpropagte the parameter(s) per batch
                    if len(loss_ev) > 0:
                        loss_ev_mean = torch.mean(torch.stack(loss_ev))
                        loss_ev_mean.backward()
                        nan_check = torch.tensor([getattr(self.sim_iter, param).grad.isnan() for param in self.relevant_params_list]).sum()
                        if nan_check == 0:
                            self.optimizer.step()
                            losses_batch.append(loss_ev_mean.item())

                    pbar.update(1)

                # Print out params at each epoch
                if epoch % print_freq == 0:
                    for param in self.relevant_params_list:
                        print(param, getattr(self.sim_physics,param).item())

                # Keep track of training history
                for param in self.relevant_params_list:
                    self.training_history[param].append(getattr(self.sim_iter, param).item())
                if len(losses_batch) > 0:
                    self.training_history['losses'].append(np.mean(losses_batch))

                # Save history in pkl files
                n_steps = len(self.training_history[param])
                if n_steps % save_freq == 0:
                    with open(f'history_epoch{n_steps}.pkl', "wb") as f_history:
                        pickle.dump(self.training_history, f_history)
                    if os.path.exists(f'history_epoch{n_steps-save_freq}.pkl'):
                        os.remove(f'history_epoch{n_steps-save_freq}.pkl') 


    def loss_scan(self, dataloader, param_range=None, n_steps=10, shuffle=False, save_freq=5, print_freq=1):

        if len(self.relevant_params_list) > 1: 
            raise NotImplementedError("Can't do loss scan for more than one variable at a time!")

        param = self.relevant_params_list[0]
        scan_losses = []
        scan_grads = []
        if param_range is None:
            param_range = [ranges[param]['down'], ranges[param]['up']]
        param_vals = torch.linspace(param_range[0], param_range[1], n_steps)

        # make a folder for the pixel target
        if os.path.exists(f'target_{param}'):
            shutil.rmtree(f'target_{param}', ignore_errors=True)
        os.makedirs(f'target_{param}')

        # The training loop
        with tqdm(total=len(dataloader) * len(param_vals)) as pbar:
            for run_no, param_val in enumerate(param_vals):
                setattr(self.sim_iter, param, param_val/ranges[param]['nom'])
                self.sim_iter.track_gradients([param])

                # Losses for each batch -- used to compute epoch loss
                losses_batch=[]
                grads_batch = []
                for i, selected_tracks_bt_torch in enumerate(dataloader):

                    # Get rid of the extra dimension and padding elements for the loaded data
                    selected_tracks_bt_torch = torch.flatten(selected_tracks_bt_torch, start_dim=0, end_dim=1)
                    selected_tracks_bt_torch = selected_tracks_bt_torch[selected_tracks_bt_torch[:, self.track_fields.index("dx")] > 0]
                    event_id_map, unique_eventIDs = get_id_map(selected_tracks_bt_torch, self.track_fields, self.device)

                    loss_ev = []
                    # Calculate loss per event
                    for ev in unique_eventIDs:
                        selected_tracks_torch = selected_tracks_bt_torch[selected_tracks_bt_torch[:, self.track_fields.index("eventID")] == ev]
                        selected_tracks_torch = selected_tracks_torch.to(self.device)

                        if shuffle:
                            target, pix_target, ticks_list_targ = all_sim(self.sim_target, selected_tracks_torch, self.track_fields,
                                                                          event_id_map, unique_eventIDs,
                                                                          return_unique_pix=True)
                            embed_target = embed_adc_list(self.sim_target, target, pix_target, ticks_list_targ)
                        else:
                            # Simulate target and store them
                            if run_no == 0:

                                target, pix_target, ticks_list_targ = all_sim(self.sim_target, selected_tracks_torch, self.track_fields,
                                                                              event_id_map, unique_eventIDs,
                                                                              return_unique_pix=True)
                                embed_target = embed_adc_list(self.sim_target, target, pix_target, ticks_list_targ)

                                torch.save(embed_target, f'target_{param}/batch' + str(i) + '_ev' + str(int(ev))+ '_target.pt')

                            else:
                                embed_target = torch.load(f'target_{param}/batch' + str(i) + '_ev' + str(int(ev))+ '_target.pt')

                        # Undo normalization (sim -> sim_physics)
                        for param in self.relevant_params_list:
                            setattr(self.sim_physics, param, getattr(self.sim_iter, param)*ranges[param]['nom'])
                            if run_no % print_freq == 0:
                                print(param, getattr(self.sim_physics, param))

                        # Simulate and get output
                        output, pix_out, ticks_list_out = all_sim(self.sim_physics, selected_tracks_torch, self.track_fields,
                                                  event_id_map, unique_eventIDs,
                                                  return_unique_pix=True)

                        # Embed both output and target into "full" image space
                        embed_output = embed_adc_list(self.sim_physics, output, pix_out, ticks_list_out)

                        # Calc loss between simulated and target + backprop
                        loss = self.loss_fn(self.sim_physics, embed_output, embed_target)

                        # To be investigated -- sometimes we get nans. Avoid doing a step if so
                        if not loss.isnan():
                            loss_ev.append(loss)

                    # Backpropagte the parameter(s) per batch
                    if len(loss_ev) > 0:
                        loss_ev_mean = torch.mean(torch.stack(loss_ev))
                        loss_ev_mean.backward()
                        losses_batch.append(loss_ev_mean.item())
                        grads_batch.append(getattr(self.sim_iter, param).grad.item())

                    pbar.update(1)

                scan_losses.append(np.mean(losses_batch))
                scan_grads.append(np.mean(np.nan_to_num(grads_batch)))

            if run_no % save_freq == 0:
                recording = {'param' : param,
                             'param_vals': param_vals,
                             'norm_factor' : ranges[param]['nom'],
                             'target_val' : getattr(self.sim_target, param),
                             'losses' : scan_losses,
                             'grads' : scan_grads }

                outname = f"loss_scan_{param}_{param_vals[0]:.02f}_{param_vals[-1]:.02f}_{run_no}"
                with open(outname+".pkl", "wb") as f:
                    pickle.dump(recording, f)
                if os.path.exists(f'loss_scan_{param}_{param_vals[0]:.02f}_{param_vals[-1]:.02f}_{run_no-save_freq}.pkl'):
                    os.remove(f'loss_scan_{param}_{param_vals[0]:.02f}_{param_vals[-1]:.02f}_{run_no-save_freq}.pkl')

        recording = {'param' : param,
                     'param_vals': param_vals,
                     'norm_factor' : ranges[param]['nom'],
                     'target_val' : getattr(self.sim_target, param),
                     'losses' : scan_losses,
                     'grads' : scan_grads }

        outname = f"loss_scan_{param}_{param_vals[0]:.02f}_{param_vals[-1]:.02f}"
        with open(outname+".pkl", "wb") as f:
            pickle.dump(recording, f)

        return recording, outname


# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2024 sportstensor

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import asyncio
from typing import List
import datetime as dt

# Bittensor
import bittensor as bt
import torch

# Bittensor Validator Template:
from common.protocol import GetMatchPrediction
from common.constants import DATA_SYNC_INTERVAL_IN_MINUTES, VALIDATOR_TIMEOUT, NUM_MINERS_TO_SEND_TO, BASE_MINER_PREDICTION_SCORE, MAX_BATCHSIZE_FOR_SCORING, SCORING_INTERVAL_IN_MINUTES
import vali_utils as utils
from storage.sqlite_validator_storage import SqliteValidatorStorage



# import base validator class which takes care of most of the boilerplate
from base.validator import BaseValidatorNeuron


class Validator(BaseValidatorNeuron):
    """
    Your validator neuron class. You should use this class to define your validator's behavior. In particular, you should replace the forward function with your own logic.

    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a validator such as keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()

        """
        api_root = (
            "https://dev-api.sportstensor.ai/validator/"
            if self.config.subtensor.network == "test" else
            "https://api.sportstensor.ai/validator"
        )
        """
        api_root = "http://95.179.153.99:8000"
        self.match_data_endpoint = f"{api_root}/matches"

        self.client_timeout_seconds = VALIDATOR_TIMEOUT
        self.next_match_syncing_datetime = dt.datetime.now(dt.UTC)
        self.next_scoring_datetime = dt.datetime.now(dt.UTC)
        self.storage = SqliteValidatorStorage()  # Create an instance of the storage handler


    async def sync_and_store_matches(self):
        """ Sync and store match data. """
        if self.next_match_syncing_datetime <= dt.datetime.now(dt.UTC):
            bt.logging.info("Syncing the latest match data to local validator storage.")
            match_data = await utils.sync_match_data(self.match_data_endpoint)  # Await the fetching function
            if match_data:
                self.storage.insert_matches(match_data)  # Store the data
            self.next_match_syncing_datetime = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=DATA_SYNC_INTERVAL_IN_MINUTES)


    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Periodically updating match data.
        - Generating the prediction query
        - Querying the miners
        - Getting the responses and updating base scoring for returning valid prediction
        - Storing prediction responses
        - Validating past prediction responsess
        - Updating the scores
       
        The forward function is called by the validator every time step.

        It is responsible for querying the network and scoring the responses.

        Args:
            self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.
        """

        """ START MATCH SYNCING """
        # Check if we're ready to sync another batch of matches
        if self.next_match_syncing_datetime <= dt.datetime.now(dt.UTC):
            bt.logging.info(f"Syncing the latest match data to local validator storage.")
            utils.sync_match_data(self.match_data_endpoint)
            # Update next sync time
            self.next_match_syncing_datetime = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=DATA_SYNC_INTERVAL_IN_MINUTES)
        """ END MATCH SYNCING """

        """ START MATCH PREDICTION REQUESTS """
        # Get miner uids to send prediction requests to
        miner_uids = utils.get_random_uids(self, k=NUM_MINERS_TO_SEND_TO)

        if len(miner_uids) == 0:
            bt.logging.info("No miners available")
            return
        
        # Get a prediction requests to send to miners
        match_prediction_requests = utils.get_match_prediction_requests()

        # The dendrite client queries the network.
        bt.logging.info(f"Sending '{len(match_prediction_requests)}' matches to miners for predictions.")
        # Loop through predictions and send to miners
        for mpr in match_prediction_requests:
            input_synapse = GetMatchPrediction(match_prediction=mpr)
            # Send prediction requests to miners and store their responses
            finished_responses, working_miner_uids = await utils.send_predictions_to_miners(bt.wallet, bt.metagraph, miner_uids, input_synapse)

            # Adjust the scores based on responses from miners.
            try:
                rewards = (await self.get_basic_match_prediction_rewards(input_synapse=input_synapse, responses=finished_responses)).to(self.device)
            except Exception as e:
                bt.logging.error(f"Error in get_basic_match_prediction_rewards: {e}")
                return

            bt.logging.info(f"Scored responses: {rewards}")
            # Update the scores based on the rewards. You may want to define your own update_scores function for custom behavior.
            self.update_scores(rewards, working_miner_uids)
            # Update the scores of miner uids NOT working. Default to 0.
            not_working_miner_uids = []
            no_rewards = []
            for uid in miner_uids:
                if uid not in working_miner_uids:
                    not_working_miner_uids.append(uid)
                    no_rewards.append(0.0)
            self.update_scores(torch.FloatTensor(no_rewards).to(self.device), not_working_miner_uids)
        """ END MATCH PREDICTION REQUESTS """

        """ START MATCH PREDICTION SCORING """
        # Check if we're ready to score another batch of predictions
        if self.next_scoring_datetime <= dt.datetime.now(dt.UTC):
            bt.logging.info(f"Checking if there are predictions to score.")
            utils.find_match_predictions_to_score(MAX_BATCHSIZE_FOR_SCORING)
            # Update next sync time
            self.next_scoring_datetime = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=SCORING_INTERVAL_IN_MINUTES)
        """ END MATCH PREDICTION SCORING """

    async def get_basic_match_prediction_rewards(
        self,
        input_synapse: GetMatchPrediction,
        responses: List[GetMatchPrediction],
    ) -> torch.FloatTensor:
        """
        Returns a tensor of rewards for the given query and responses.
        """
        # Create a list of fixed rewards for all responses
        rewards = [BASE_MINER_PREDICTION_SCORE for _ in responses]
        return torch.FloatTensor(rewards).to(self.device)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    Validator().run()
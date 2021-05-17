import os
import sys
import json
import wandb
import numpy as np
from importlib import import_module
from sklearn.model_selection import StratifiedKFold, train_test_split
from collections import defaultdict

from data_utils import tokenize_ontology

sys.path.insert(0, "../CustomizedModule")
from CustomizedScheduler import get_scheduler
from CustomizedOptimizer import get_optimizer

import torch
import torch.nn as nn
from tqdm import tqdm

from eval_utils import DSTEvaluator
from evaluation import _evaluation
from inference import inference_SUMBT
from data_utils import train_data_loading, get_data_loader

from preprocessor import SUMBTPreprocessor
from model import SUMBT

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
n_gpu = 1 if torch.cuda.device_count() < 2 else torch.cuda.device_count()

def get_informations(args):
    # Define Tokenizer
    tokenizer_module = getattr(
        import_module("transformers"), f"{args.model_name}Tokenizer"
    )
    tokenizer = tokenizer_module.from_pretrained(args.pretrained_name_or_path)

    slot_meta, train_examples, train_labels = train_data_loading(args, isUserFirst=True, isDialogueLevel=True)
    ontology = json.load(open("../input/data/train_dataset/ontology.json"))

    # Define Preprocessor
    max_turn = max([len(e)*2 for e in train_examples])
    processor = SUMBTPreprocessor(slot_meta,
                                tokenizer,
                                ontology=ontology,  # predefined ontology
                                max_seq_length=args.max_seq_length,  # 각 turn마다 최대 길이
                                max_turn_length=max_turn)  # 각 dialogue의 최대 turn 길이

    # Extract Features
    train_features = processor.convert_examples_to_features(train_examples)

    slot_type_ids, slot_values_ids = tokenize_ontology(ontology, tokenizer, args.max_label_length)
    num_labels = [len(s) for s in slot_values_ids] # 각 Slot 별 후보 Values의 갯수

    json.dump(
        vars(args),
        open(f"{args.model_dir}/{args.model_fold}/exp_config.json", "w"),
        indent=2,
        ensure_ascii=False,
    )
    json.dump(
        slot_meta,
        open(f"{args.model_dir}/{args.model_fold}/slot_meta.json", "w"),
        indent=2,
        ensure_ascii=False,
    )
    return processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, train_features, train_labels


def select_kfold_or_full(args, processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, features, labels):
    features = np.array(features)
    dialogue_labels, domain_labels = defaultdict(list), []

    domain_labels = [len(f.domain)-1 for f in features]

    for k, v in labels.items():
        dialogue_labels['-'.join(k.split('-')[:-1])].append([k, v])

    if args.isKfold:
        kf = StratifiedKFold(n_splits=args.fold_num, random_state=args.seed, shuffle=True)
        fold_idx = 1
        
        for train_index, dev_index in kf.split(features, domain_labels):
            os.makedirs(f'{args.model_dir}/{args.model_fold}/{fold_idx}-fold', exist_ok=True)

            train_features, dev_features = features[train_index.astype(int)], features[dev_index.astype(int)]
            dev_dialogue_labels = np.array(list(dialogue_labels.items()))[dev_index.astype(int)]
        
            dev_labels = {t[0]:t[1] for turn in dev_dialogue_labels[:, 1] for t in turn}
            
            train_loader = get_data_loader(processor, train_features, args.train_batch_size)
            dev_loader = get_data_loader(processor, dev_features, args.eval_batch_size)

            print(f"========= {fold_idx} fold =========")
            train_model(args, processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, fold_idx, train_loader, dev_loader, dev_labels)
            fold_idx += 1
    else:
        fold_idx = None
        train_index, dev_index = train_test_split(np.array(range(len(features))), test_size=0.1, random_state=args.seed, stratify=domain_labels)

        train_features, dev_features = features[train_index.astype(int)], features[dev_index.astype(int)]
        dev_dialogue_labels = np.array(list(dialogue_labels.items()))[dev_index.astype(int)]
        
        dev_labels = {t[0]:t[1] for turn in dev_dialogue_labels[:, 1] for t in turn}

        train_loader = get_data_loader(processor, train_features, args.train_batch_size)
        dev_loader = get_data_loader(processor, dev_features, args.eval_batch_size)

        train_model(args, processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, fold_idx, train_loader, dev_loader, dev_labels)

def train_model(args, processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, fold_idx, train_loader, dev_loader, dev_labels):
    model = SUMBT(args, num_labels, device)
    model.initialize_slot_value_lookup(slot_values_ids, slot_type_ids)  # Tokenized Ontology의 Pre-encoding using BERT_SV
    model.to(device)
    print("Model is initialized")

    """## Optimizer & Scheduler 선언 """
    n_epochs = args.epochs
    t_total = len(train_loader) * n_epochs
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

    optimizer = get_optimizer(optimizer_grouped_parameters, args)  # get optimizer (Adam, sgd, AdamP, ..)

    scheduler = get_scheduler(
        optimizer, t_total, args
    )  # get scheduler (custom, linear, cosine, ..)

    best_score, best_checkpoint = 0, 0
    for epoch in range(n_epochs):
        batch_loss = []
        model.train()
        for step, batch in enumerate(train_loader):
            input_ids, segment_ids, input_masks, target_ids, num_turns, guids  = \
            [b.to(device) if not isinstance(b, list) else b for b in batch]

            # Forward
            if n_gpu == 1:
                loss, loss_slot, acc, acc_slot, _ = model(input_ids, segment_ids, input_masks, target_ids, n_gpu)
            else:
                loss, _, acc, acc_slot, _ = model(input_ids, segment_ids, input_masks, target_ids, n_gpu)
            
            batch_loss.append(loss.item())

            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            for learning_rate in scheduler.get_lr():
                    wandb.log({"learning_rate": learning_rate})
            
            optimizer.zero_grad()

            if step % 100 == 0:
                print('[%d/%d] [%d/%d] %f' % (epoch, n_epochs, step, len(train_loader), loss.item()))

                wandb.log({
                            "epoch": epoch,
                            "Train epoch loss": loss.item()})
        
        predictions = inference_SUMBT(model, dev_loader, processor, device)
        eval_result = _evaluation(predictions, dev_labels, slot_meta)
        
        for k, v in eval_result.items():
            print(f"{k}: {v}")
        
        if best_score < eval_result["joint_goal_accuracy"]:
            print("Update Best checkpoint!")
            best_score = eval_result["joint_goal_accuracy"]
            best_checkpoint = epoch

            wandb.log({
                "epoch": epoch, 
                "Best joint goal accuracy": best_score, 
                "Best turn slot accuracy": eval_result['turn_slot_accuracy'],
                "Best turn slot f1": eval_result['turn_slot_f1']
            })

        if args.isKfold:
            torch.save(
                model.state_dict(), f"{args.model_dir}/{args.model_fold}/{fold_idx}-fold'/model-{epoch}.bin"
            )
        else:
            torch.save(
                model.state_dict(), f"{args.model_dir}/{args.model_fold}/model-{epoch}.bin"
            )
    
    print(f"Best checkpoint: {args.model_dir}/model-{best_checkpoint}.bin")
    wandb.log({"Best checkpoint": f"{args.model_dir}/model-{best_checkpoint}.bin"})


def train(args):
    processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, train_features, train_labels = get_informations(args)
    select_kfold_or_full(args, processor, slot_meta, num_labels, slot_values_ids, slot_type_ids, train_features, train_labels)

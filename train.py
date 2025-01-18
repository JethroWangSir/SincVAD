import os
import logging
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import wandb
from tqdm import tqdm

from dataset import SCF_NeMo, AVA_Tuple
from model.sincvad import SincVAD
from function.util import WarmupHoldDecayScheduler, save_best_k_model_with_auroc, median_smoothing_filter, metrics_calculation
from function.loss import AUROCLoss

# Set GPU
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

WINDOW_SIZE = 0.63
SINC_CONV = True
AUROC_LOSS_WEIGHT = 0.05
AUROC_LOSS_TYPE = 'psq'

if WINDOW_SIZE == 0.63:
    batch_size = 256
    overlap = 0.875
    patch_size = 8
    median_kernel_size = 7
elif WINDOW_SIZE == 0.16:
    batch_size = 1024
    overlap = 0.8
    patch_size = 2
    median_kernel_size = 11
elif WINDOW_SIZE == 0.032:
    batch_size = 5120
    overlap = 0.0
    patch_size = 1
    median_kernel_size = 21

if SINC_CONV and AUROC_LOSS_WEIGHT > 0.0:
    name = f'exp_{WINDOW_SIZE}_sinc_tinyvad_{AUROC_LOSS_TYPE}_{AUROC_LOSS_WEIGHT}'
    max_duration = 30.0
elif SINC_CONV:
    name = f'exp_{WINDOW_SIZE}_sinc_tinyvad'
    max_duration = 30.0
elif AUROC_LOSS_WEIGHT > 0.0:
    name = f'exp_{WINDOW_SIZE}_tinyvad_{AUROC_LOSS_TYPE}_{AUROC_LOSS_WEIGHT}'
    max_duration = 300.0
else:
    name = f'exp_{WINDOW_SIZE}_tinyvad'
    max_duration = 300.0

exp_dir = f'./exp/{name}/'
os.makedirs(exp_dir, exist_ok=True)

# Initialize wandb
wandb.init(project="SincVAD", name=name, config={
    "seed": 42,
    "epochs": 150,
    "batch_size": batch_size,
    "max_lr": 0.01,
    "momentum": 0.9,
    "weight_decay": 0.001,
    "warmup_ratio": 0.05,
    "hold_ratio": 0.45,
    "min_lr": 0.001,
    "augment": True,
    "window_size": WINDOW_SIZE,
    "sinc_conv": SINC_CONV,
    "auroc_loss_weight": AUROC_LOSS_WEIGHT,
    "auroc_loss_type": AUROC_LOSS_TYPE,
})
config = wandb.config
torch.manual_seed(config.seed)

# Setup logging
log_file = os.path.join(exp_dir, 'train.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)

# Load datasets
train_manifests = [f'./data/manifest/{WINDOW_SIZE}/balanced_background_training_manifest.json', f'./data/manifest/{WINDOW_SIZE}/balanced_speech_training_manifest.json']
val_dir = '/share/nas165/aaronelyu/Datasets/AVA-speech/'

logging.info('Loading training set ...')
train_dataset = SCF_NeMo(
    manifest_files=train_manifests,
    sample_duration=config.window_size,
    augment=config.augment,
    feature_extraction=(not config.sinc_conv),
    )
train_loader = DataLoader(train_dataset, batch_size=config.batch_size, shuffle=True, num_workers=8, pin_memory=True)
logging.info(f'Training set size: {len(train_loader)}')

logging.info('Loading validation set...')
val_dataset = AVA_Tuple(
    root_dir=val_dir,
    max_duration=max_duration,
    sample_duration=config.window_size,
    overlap=overlap,
    feature_extraction=(not config.sinc_conv),
)
val_loader = DataLoader(val_dataset, batch_size=1, shuffle=False)
logging.info(f'Validation set size: {len(val_loader)}')

logging.info('Finish loading dataset!')
print('------------------------------')

# Initialize model, loss function, and optimizer
model = SincVAD(1, 32, 64, patch_size, 2, config.sinc_conv).to(device)
bce_criterion = nn.BCEWithLogitsLoss()
auroc_criterion = AUROCLoss()
optimizer = optim.SGD(
    model.parameters(),
    lr=config.max_lr,
    momentum=config.momentum,
    weight_decay=config.weight_decay
)
scheduler = WarmupHoldDecayScheduler(
    optimizer=optimizer,
    total_steps=len(train_loader) * config.epochs,
    warmup_ratio=config.warmup_ratio,
    hold_ratio=config.hold_ratio,
    min_lr=config.min_lr,
    max_lr=config.max_lr
)

# Training loop
top_3_val_auroc = []

for epoch in range(config.epochs):
    model.train()
    running_loss = 0.0
    val_probs_list, val_labels_list = [], []

    train_progress_bar = tqdm(enumerate(train_loader), total=len(train_loader), desc=f'Epoch [{epoch + 1}/{config.epochs}] Training')

    for batch_idx, batch in train_progress_bar:
        inputs, labels = batch[0].to(device), batch[1].float().unsqueeze(1).to(device)
        # print(f'Inputs: {inputs.shape}, Labels: {labels}')

        optimizer.zero_grad()

        outputs = model(inputs)

        # print(f'Outputs: {outputs.shape}, Labels: {labels.shape}')
        bce_loss = bce_criterion(outputs, labels)
        auroc_loss = auroc_criterion(outputs, labels)
        loss = (1 - AUROC_LOSS_WEIGHT) * bce_loss + AUROC_LOSS_WEIGHT * auroc_loss

        loss.backward()
        optimizer.step()
        scheduler.step()

        if batch_idx % 10 == 0:
            wandb.log({
                "train_loss": loss.item(),
                "bce_loss": (1 - AUROC_LOSS_WEIGHT) * bce_loss.item(),
                "auroc_loss": AUROC_LOSS_WEIGHT * auroc_loss,
                "learning_rate": scheduler.get_last_lr()[0]
            })
        
        running_loss += loss.item()

    avg_train_loss = running_loss / len(train_loader)
    logging.info(f'Epoch [{epoch + 1}/{config.epochs}] Train Loss: {avg_train_loss:.4f}')
    wandb.log({"epoch": epoch + 1, "avg_train_loss": avg_train_loss})

    # Validation step
    model.eval()

    with torch.no_grad():
        for batch in tqdm(val_loader, desc='Validating'):
            if config.sinc_conv:
                val_inputs = [item[0].to(device) for item in batch]
            else:
                val_inputs = [item[1].to(device) for item in batch]
            val_labels = [item[2].to(device).float().unsqueeze(1) for item in batch]
            val_inputs = torch.cat(val_inputs, dim=0)
            val_labels = torch.cat(val_labels, dim=0)

            val_probs = model.predict(val_inputs)

            # Apply median smoothing filter
            val_probs_list, val_labels_list = median_smoothing_filter(val_probs, val_labels, val_probs_list, val_labels_list, median_kernel_size, device)

    # Concatenate results
    val_labels_cat = torch.cat(val_labels_list, dim=0).cpu().numpy()
    val_probs_cat = torch.cat(val_probs_list, dim=0).cpu().numpy()

    # Metrics calculation
    auroc, fpr, fnr, f2_score = metrics_calculation(val_labels_cat, val_probs_cat, 0.5)
    logging.info(f'Epoch [{epoch + 1}/{config.epochs}] AUROC: {auroc}, FPR: {fpr}, FNR: {fnr}, F2-score: {f2_score}')
    wandb.log({"auroc": auroc, "fpr": fpr, "fnr": fnr, "f2-score": f2_score})

    # Save the best 3 models
    save_best_k_model_with_auroc(exp_dir, model, epoch, auroc, top_3_val_auroc, k=3)

# After last epoch, save final model
final_checkpoint = os.path.join(exp_dir, f'model_last_epoch.ckpt')
torch.save(model.state_dict(), final_checkpoint)
logging.info(f'Final model saved to {final_checkpoint}')

logging.info('Training complete!')
wandb.finish()
